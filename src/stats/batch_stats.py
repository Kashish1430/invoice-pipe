"""Batch data-quality statistics: scheduled scan + compute in plain Python.

DynamoDB has no native aggregation, so the MVP reads the items and adds them up
here. At the current scale -- six samples growing to low thousands of invoices --
a filtered `Scan` costs a handful of RCUs and returns sub-second, with no extra
infrastructure to run or pay for.

The migration trigger is explicit rather than aspirational: move to an Athena
query over a DynamoDB S3 export once the table passes roughly 1M items, or once
stats need sub-minute freshness across full history. Below that line, exporting
and querying costs more than it saves.

Month-scoped runs go through GSI1 rather than a full Scan, so the routine
"stats for last month" job already reads only the partitions it needs.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from src.validation import dq_rules

MIGRATION_ITEM_THRESHOLD = 1_000_000


@dataclass
class BatchStats:
    items_scanned: int = 0
    invoice_records: int = 0
    quarantine_tombstones: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    dq_flag_counts: dict[str, int] = field(default_factory=dict)
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    by_vendor: dict[str, int] = field(default_factory=dict)
    by_month: dict[str, int] = field(default_factory=dict)
    total_gbp: Decimal = Decimal("0")
    by_original_currency: dict[str, int] = field(default_factory=dict)
    clean_rate: float = 0.0
    exceeds_migration_threshold: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items_scanned": self.items_scanned,
            "invoice_records": self.invoice_records,
            "quarantine_tombstones": self.quarantine_tombstones,
            "by_status": self.by_status,
            "dq_flag_counts": self.dq_flag_counts,
            "quarantine_reasons": self.quarantine_reasons,
            "by_vendor": self.by_vendor,
            "by_month": self.by_month,
            "total_gbp": str(self.total_gbp),
            "by_original_currency": self.by_original_currency,
            "clean_rate": round(self.clean_rate, 4),
            "exceeds_migration_threshold": self.exceeds_migration_threshold,
        }


def _is_tombstone(item: dict[str, Any]) -> bool:
    return str(item.get("PK", "")).startswith("QUARANTINE#")


def compute(items: Iterable[dict[str, Any]]) -> BatchStats:
    """Aggregate already-fetched items. Pure, so it is trivially testable."""
    stats = BatchStats()
    status = Counter()
    flags = Counter()
    reasons = Counter()
    vendors = Counter()
    months = Counter()
    currencies = Counter()

    for item in items:
        stats.items_scanned += 1
        if _is_tombstone(item):
            stats.quarantine_tombstones += 1
            status["QUARANTINED"] += 1
            reasons[str(item.get("error_type", "Unknown"))] += 1
            continue

        stats.invoice_records += 1
        status[str(item.get("status", "UNKNOWN"))] += 1
        for flag in item.get("dq_flags") or []:
            flags[str(flag)] += 1
        vendors[str(item.get("PK", "")).removeprefix("VENDOR#")] += 1
        months[str(item.get("GSI1PK", "")).removeprefix("DATE#")] += 1
        currencies[str(item.get("original_currency", "UNKNOWN"))] += 1

        total = item.get("total_amount")
        if isinstance(total, Decimal):
            stats.total_gbp += total

    stats.by_status = dict(status)
    stats.dq_flag_counts = dict(flags)
    stats.quarantine_reasons = dict(reasons)
    stats.by_vendor = dict(vendors)
    stats.by_month = dict(months)
    stats.by_original_currency = dict(currencies)
    stats.clean_rate = (
        status.get("LOADED", 0) / stats.invoice_records if stats.invoice_records else 0.0
    )
    stats.exceeds_migration_threshold = stats.items_scanned >= MIGRATION_ITEM_THRESHOLD
    return stats


#: Money attributes are written to the gold mirror as strings, since JSON has no
#: decimal type. They are rehydrated on read so the aggregate never touches float.
_MONEY_ATTRS = (
    "product_amount",
    "total_amount",
    "vat_tax_amount",
    "coupon_discount_amount",
    "original_product_amount",
    "original_total_amount",
    "original_vat_tax_amount",
    "original_coupon_discount_amount",
)


def load_gold(path: str | Path) -> list[dict[str, Any]]:
    """Read a `data/gold/<batch>.jsonl` mirror back into item dicts.

    Lets the stats job aggregate a specific batch straight from its gold
    mirror, without a full-table DynamoDB scan.
    """
    items: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            for attr in _MONEY_ATTRS:
                if isinstance(item.get(attr), str):
                    item[attr] = Decimal(item[attr])
            items.append(item)
    return items


def collect(store: Any, year_month: str | None = None) -> BatchStats:
    """Read from storage, then aggregate.

    A month argument turns the read into a GSI1 Query over a single partition;
    without one it is a full Scan, which is the deliberate MVP tradeoff above.
    """
    items = store.query_month(year_month) if year_month else store.scan_all()
    return compute(items)


def known_flags() -> tuple[str, ...]:
    """Every soft-warn flag, so a report can show explicit zeroes."""
    return dq_rules.SOFT_WARN_RULES


def report(stats: BatchStats) -> str:
    lines = [
        f"records: {stats.invoice_records}  quarantined: {stats.quarantine_tombstones}",
        f"clean rate: {stats.clean_rate:.1%}",
        f"total (GBP): {stats.total_gbp}",
        "",
        "status:",
    ]
    for k, v in sorted(stats.by_status.items()):
        lines.append(f"  {k:<24} {v}")
    lines.append("")
    lines.append("dq flags:")
    for name in known_flags():
        lines.append(f"  {name:<28} {stats.dq_flag_counts.get(name, 0)}")
    if stats.quarantine_reasons:
        lines.append("")
        lines.append("quarantine reasons:")
        for k, v in sorted(stats.quarantine_reasons.items()):
            lines.append(f"  {k:<28} {v}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI glue
    from src.storage.dynamo_client import DynamoStore

    parser = argparse.ArgumentParser(description="Compute batch DQ statistics.")
    parser.add_argument("--month", default=None, help="yyyy-mm; queries GSI1")
    parser.add_argument(
        "--gold",
        default=None,
        help="aggregate a data/gold/<batch>.jsonl mirror instead of DynamoDB",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    stats = (
        compute(load_gold(args.gold))
        if args.gold
        else collect(DynamoStore(), args.month)
    )
    print(json.dumps(stats.to_dict(), indent=2) if args.json else report(stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
