"""Currency conversion helpers.

Rates live in config/fx_rates.yaml and are expressed as *units of the source
currency per 1 GBP*, so conversion is always a division:

    gbp_amount = source_amount / rate

The MVP uses one fixed rate table per run rather than a date-keyed lookup: all
six samples are INR and a moving rate would make batch replays non-reproducible.
`fx_rate_applied` / `fx_rate_date` are stamped onto every record so a later
switch to per-date rates stays auditable against what was actually loaded.
"""

from __future__ import annotations

from datetime import date as date_, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import Any

import yaml

from src import settings

TARGET_CURRENCY = "GBP"
CENTS = Decimal("0.01")


class UnknownCurrencyError(ValueError):
    """Raised when no rate exists for a currency - a hard failure, not a guess."""


@lru_cache(maxsize=None)
def _table() -> dict[str, Any]:
    with open(settings.fx_rates_path(), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def reset_cache() -> None:
    _table.cache_clear()


def rate_date() -> date_:
    """The `as_of` date of the loaded table, stamped as fx_rate_date."""
    raw = _table().get("as_of")
    if isinstance(raw, date_):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if raw:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    return date_.today()


def rate_for(currency: str) -> Decimal:
    """Units of `currency` per 1 GBP. GBP itself is 1."""
    code = (currency or "").strip().upper()
    if not code:
        raise UnknownCurrencyError("empty currency code")
    rates = _table().get("rates") or {}
    key = f"{code}_{TARGET_CURRENCY}"
    if key not in rates:
        raise UnknownCurrencyError(
            f"no FX rate for {code!r} in {settings.fx_rates_path()} (looked for {key!r})"
        )
    # str() first: YAML floats would reintroduce the binary rounding that the
    # Decimal-for-money decision exists to avoid.
    rate = Decimal(str(rates[key]))
    if rate <= 0:
        raise UnknownCurrencyError(f"non-positive FX rate for {code!r}: {rate}")
    return rate


def to_gbp(amount: Decimal, rate: Decimal) -> Decimal:
    """Divide and round half-up to 2dp - the convention money reporting expects."""
    if rate <= 0:
        raise UnknownCurrencyError(f"non-positive FX rate: {rate}")
    return (Decimal(amount) / Decimal(rate)).quantize(CENTS, rounding=ROUND_HALF_UP)


def conversion_context(currency: str) -> dict[str, Any]:
    """Everything the ingestion glue injects into InvoiceRecord for provenance."""
    return {"fx_rate_applied": rate_for(currency), "fx_rate_date": rate_date()}
