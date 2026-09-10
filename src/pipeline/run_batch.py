"""Batch orchestrator: discover -> stage -> extract -> map -> validate -> load.

The controlling property is per-file isolation. Every document's full Stage 1-4
run sits inside its own exception boundary, so a corrupt PDF, a Textract
timeout, a schema-violating tool call or a negative total takes exactly one file
out of the batch and the other files still load. There is no path by which one
bad invoice aborts the run.

Four outcomes, and only one of them is a failure:

    LOADED / LOADED_WITH_WARNINGS  written to DynamoDB, mirrored to data/gold
    DUPLICATE_SKIPPED              already loaded by an earlier run; not an error
    QUARANTINED                    hard-fail; sidecar written, raw PDF untouched
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import date as date_, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src import settings
from src.extraction.raw_shapes import ExtractedDocument, normalize_label
from src.extraction.textract_client import TextractClient, check_document_size
from src.ingestion.file_discovery import DiscoveredFile, discover_files, new_batch_id
from src.ingestion.s3_discovery import (
    S3DiscoveredFile,
    discover_s3_files,
    mirror_to_local,
    resolve_batch_date,
)
from src.ingestion.s3_uploader import S3Uploader, StagedObject
from src.mapping.bedrock_client import BedrockMapper, MappedPayload
from src.mapping.vendor_alias_cache import VendorAliasCache
from src.pipeline.errors import (
    DuplicateRecordError,
    PipelineError,
    TextractExtractionError,
)
from src.quarantine.sidecar_writer import write_quarantine_sidecar
from src.storage.dynamo_client import DynamoStore
from src.validation import dq_rules, fx
from src.validation.models import InvoiceRecord

log = logging.getLogger("invoice_pipe")

LOADED = "LOADED"
LOADED_WITH_WARNINGS = "LOADED_WITH_WARNINGS"
DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"
QUARANTINED = "QUARANTINED"


# ---------------------------------------------------------------------------
# silver persistence -- replay/debug without re-calling AWS
# ---------------------------------------------------------------------------
def silver_path(
    source_file: str,
    suffix: str,
    silver_dir: Path | None = None,
    batch_date: date_ | None = None,
) -> Path:
    """`data/silver/<file>.<suffix>.json`, or `data/silver/<y>/<m>/<d>/<file>.<suffix>.json`
    when `batch_date` is given -- mirroring `data/raw/<y>/<m>/<d>/` so a
    document's raw file and its silver output partition the same way. Local
    (non-S3) discovery has no batch date, so it stays flat, unchanged from
    before this partitioning existed.
    """
    base = silver_dir or settings.data_path("silver")
    if batch_date is not None:
        base = base / str(batch_date.year) / str(batch_date.month) / str(batch_date.day)
    return base / f"{Path(source_file).stem}.{suffix}.json"


def write_silver(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return path


def append_gold(item: dict[str, Any], batch_id: str, gold_dir: Path | None = None) -> Path:
    """Mirror what was actually loaded, keyed by dedup_key.

    The gold layer is a flat-file replica of storage, not a second source of
    truth: it makes a batch's output diffable and greppable without a DynamoDB
    round trip.
    """
    base = gold_dir or settings.data_path("gold")
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"{batch_id}.jsonl"
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    return out


# ---------------------------------------------------------------------------
# ingestion glue: mapped payload -> typed record
# ---------------------------------------------------------------------------
def resolve_confidences(
    payload: MappedPayload, doc: ExtractedDocument
) -> dict[str, float]:
    """Canonical field -> the Textract confidence of the value behind it.

    Bedrock reports which source label each value came from; that label is
    looked back up in Textract's own scores. The confidence therefore always
    describes the OCR of the value that was actually stored, not an average
    over the document.

    Two lookups, tried in order, both keyed on the same normalized label
    (`raw_shapes.normalize_label`) so a source label Bedrock reasonably
    cleaned up -- stripped of an OCR'd stray bullet or collapsed whitespace
    -- still matches Textract's raw one:
      1. summary fields (header/footer values -- vendor, total, dates, ...)
      2. line-item columns (a table row's cells have no summary-field
         entry at all; without this a value like product_amount, sourced
         from a line item's "Unit Price" column, would never get a score)
    """
    by_summary_label = doc.confidence_by_label()
    by_line_item_label = doc.line_item_confidence_by_label()
    out: dict[str, float] = {}
    for canonical, label in payload.source_labels.items():
        key = normalize_label(str(label))
        conf = by_summary_label.get(key)
        if conf is None:
            conf = by_line_item_label.get(key)
        if conf is not None:
            out[canonical] = float(conf)
    return out


def build_record(
    payload: MappedPayload,
    doc: ExtractedDocument,
    source_file: str,
) -> InvoiceRecord:
    """Assemble the Pydantic record, injecting FX and provenance.

    The same raw money strings are passed to both the canonical field and its
    `original_*` twin, and both go through the identical coercion validator.
    Only the canonical half is then converted, which is what makes the transform
    reversible: `original_x / fx_rate_applied == x`.
    """
    f = payload.fields
    source_currency = (f.get("currency") or "").strip()
    fx_context = fx.conversion_context(source_currency)

    return InvoiceRecord(
        date=f.get("date"),
        company_name=f.get("company_name"),
        buyer_name=f.get("buyer_name"),
        invoice_reference_number=f.get("invoice_reference_number"),
        product_name=f.get("product_name"),
        product_amount=f.get("product_amount"),
        total_amount=f.get("total_amount"),
        vat_tax_label=f.get("vat_tax_label"),
        vat_tax_percentage=f.get("vat_tax_percentage"),
        vat_tax_amount=f.get("vat_tax_amount"),
        coupon_discount_amount=f.get("coupon_discount_amount"),
        mode_of_payment=f.get("mode_of_payment"),
        currency=source_currency,
        original_currency=source_currency,
        original_total_amount=f.get("total_amount"),
        original_product_amount=f.get("product_amount"),
        original_vat_tax_amount=f.get("vat_tax_amount"),
        original_coupon_discount_amount=f.get("coupon_discount_amount"),
        fx_rate_applied=fx_context["fx_rate_applied"],
        fx_rate_date=fx_context["fx_rate_date"],
        source_file=source_file,
        extraction_confidence=resolve_confidences(payload, doc),
        unmapped_metadata=payload.unmapped_metadata,
    )


# ---------------------------------------------------------------------------
# per-file processing
# ---------------------------------------------------------------------------
@dataclass
class Dependencies:
    """Everything the pipeline talks to, injected so tests can swap any leg."""

    extractor: Any
    mapper: Any
    store: Any
    uploader: Any | None = None
    silver_dir: Path | None = None
    gold_dir: Path | None = None
    quarantine_dir: Path | None = None

    @classmethod
    def build(cls) -> "Dependencies":
        """Always the real AWS-backed legs -- Textract, Bedrock, DynamoDB, S3.

        No offline mode: this pipeline processes whatever is actually in
        data/raw or the S3 date partition, whether that's 1 file or 500,
        against real infrastructure every time it runs.
        """
        return cls(
            extractor=TextractClient(),
            mapper=BedrockMapper(cache=VendorAliasCache()),
            store=DynamoStore(),
            uploader=S3Uploader(),
        )


@dataclass
class FileOutcome:
    filename: str
    outcome: str
    dq_flags: list[str] = field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    dedup_key: str | None = None
    sidecar: str | None = None


@dataclass
class BatchResult:
    batch_id: str
    started_utc: str
    outcomes: list[FileOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.outcome] = out.get(o.outcome, 0) + 1
        return out

    def flag_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            for flag in o.dq_flags:
                out[flag] = out.get(flag, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "started_utc": self.started_utc,
            "files": len(self.outcomes),
            "counts": self.counts(),
            "dq_flags": self.flag_counts(),
            "outcomes": [o.__dict__ for o in self.outcomes],
        }


def process_one(
    discovered: DiscoveredFile | S3DiscoveredFile, deps: Dependencies, batch_id: str
) -> FileOutcome:
    """Full Stage 1-4 run for a single document. Raises on hard failure."""
    source_file = discovered.filename
    if not discovered.has_pdf_magic:
        raise TextractExtractionError(
            f"{source_file}: not a PDF (missing %PDF- header); refusing to stage"
        )
    check_document_size(discovered.size_bytes, source_file)

    batch_date: date_ | None = getattr(discovered, "batch_date", None)

    if isinstance(discovered, S3DiscoveredFile):
        # Already lives in S3, partitioned by business date -- nothing to
        # upload. Reference the object directly where Textract will read it.
        staged = StagedObject(bucket=discovered.bucket, key=discovered.key)
    elif deps.uploader is not None:
        staged = deps.uploader.stage(discovered.path, batch_id)
    else:
        # No uploader configured on `deps` -- only reachable when a test
        # constructs Dependencies directly without one; production always
        # gets a real S3Uploader from Dependencies.build().
        staged = StagedObject(bucket="unstaged", key=source_file)

    doc = deps.extractor.analyze(staged, source_file)
    write_silver(
        doc.to_dict(),
        silver_path(source_file, "textract", deps.silver_dir, batch_date),
    )

    payload = deps.mapper.map_document(doc)
    write_silver(
        payload.to_dict(),
        silver_path(source_file, "mapped", deps.silver_dir, batch_date),
    )

    record = build_record(payload, doc, source_file)
    record.add_flags(
        dq_rules.evaluate_contextual(
            record,
            raw_date=payload.fields.get("date"),
            multiple_total_candidates=payload.multiple_total_candidates,
        )
    )

    item = deps.store.put_record(record)
    append_gold(item, batch_id, deps.gold_dir)
    return FileOutcome(
        filename=source_file,
        outcome=record.status,
        dq_flags=list(record.dq_flags),
        dedup_key=item["dedup_key"],
    )


def _source_ref(discovered: DiscoveredFile | S3DiscoveredFile) -> str:
    """A stable string naming where the file came from, for the sidecar.

    Local files have a `.path`; S3-sourced ones have no local path at all --
    their only home is a bucket + key, rendered as a URI instead.
    """
    if isinstance(discovered, S3DiscoveredFile):
        return discovered.uri
    return str(discovered.path)


def _quarantine(
    discovered: DiscoveredFile | S3DiscoveredFile,
    exc: BaseException,
    deps: Dependencies,
    batch_id: str,
    *,
    error_type: str | None = None,
) -> FileOutcome:
    payload = _last_mapped_payload(discovered, deps)
    sidecar = write_quarantine_sidecar(
        _source_ref(discovered),
        exc,
        batch_id,
        raw_mapped_payload=payload,
        error_type=error_type,
        quarantine_dir=deps.quarantine_dir,
    )
    # Tombstone so pipeline status is answerable from storage alone; a store
    # that is itself the reason for the failure must not take the batch down.
    try:
        deps.store.put_quarantine_tombstone(
            filename=discovered.filename,
            batch_id=batch_id,
            error_type=error_type or type(exc).__name__,
            failed_at_stage=getattr(exc, "stage", "pydantic_validation"),
        )
    except Exception as tombstone_exc:  # pragma: no cover - defensive
        log.warning("tombstone write failed for %s: %s", discovered.filename, tombstone_exc)

    return FileOutcome(
        filename=discovered.filename,
        outcome=QUARANTINED,
        error_type=error_type or type(exc).__name__,
        error_message=str(exc),
        sidecar=str(sidecar),
    )


def _last_mapped_payload(
    discovered: DiscoveredFile | S3DiscoveredFile, deps: Dependencies
) -> dict[str, Any]:
    """The pre-Pydantic dict, for the sidecar -- present only if mapping got that far."""
    batch_date = getattr(discovered, "batch_date", None)
    path = silver_path(discovered.filename, "mapped", deps.silver_dir, batch_date)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):  # pragma: no cover - defensive
        return {}


def _run_over(
    discovered_iter: Any, deps: Dependencies, batch_id: str, limit: int | None
) -> BatchResult:
    """Shared per-file loop for both `run_batch()` and `run_s3_batch()`.

    Identical exception handling regardless of where discovery sourced the
    file from -- `process_one()` and `_quarantine()` already branch on the
    discovered-file type where it matters (staging, sidecar naming), so the
    orchestration loop itself doesn't need to know or care.
    """
    result = BatchResult(
        batch_id=batch_id,
        started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    deps.store.create_table_if_missing()

    for index, discovered in enumerate(discovered_iter):
        if limit is not None and index >= limit:
            break
        try:
            outcome = process_one(discovered, deps, batch_id)
        except DuplicateRecordError as exc:
            # Valid record, already loaded. Not a failure and not a quarantine:
            # this is what makes re-running a batch safe.
            log.info("duplicate skipped: %s", discovered.filename)
            outcome = FileOutcome(
                filename=discovered.filename,
                outcome=DUPLICATE_SKIPPED,
                error_message=str(exc),
            )
        except (ValidationError, PipelineError) as exc:
            log.warning("quarantined %s: %s", discovered.filename, exc)
            outcome = _quarantine(discovered, exc, deps, batch_id)
        except Exception as exc:  # defensive: an unknown failure stays per-file
            log.exception("unhandled failure on %s", discovered.filename)
            outcome = _quarantine(
                discovered, exc, deps, batch_id, error_type="UnhandledException"
            )
        result.outcomes.append(outcome)

    return result


def run_batch(
    raw_dir: str | Path | None = None,
    deps: Dependencies | None = None,
    batch_id: str | None = None,
    limit: int | None = None,
) -> BatchResult:
    """Discover PDFs from a local directory (`data/raw` by default)."""
    deps = deps or Dependencies.build()
    batch_id = batch_id or new_batch_id()
    return _run_over(discover_files(raw_dir), deps, batch_id, limit)


def run_s3_batch(
    bucket: str | None = None,
    batch_date: Any = None,
    deps: Dependencies | None = None,
    batch_id: str | None = None,
    limit: int | None = None,
    s3_client: Any = None,
    mirror_local: bool = True,
    local_root: Any = None,
) -> BatchResult:
    """Discover PDFs already sitting in S3 under one day's partition.

    `batch_date` follows `s3_discovery.resolve_batch_date()`'s precedence: an
    explicit value here, else `pipeline.batch_date_override` in settings,
    else the real wall-clock date -- so a plain `run_s3_batch()` call in
    production processes *today's* partition, never a hardcoded one.

    Discovery always makes a real S3 `list_objects_v2` call -- there is no
    local directory to fall back to here.

    `mirror_local=True` (the default) copies every discovered file to
    `<local_root>/<year>/<month>/<day>/` (default `data/raw`) via
    `s3_discovery.mirror_to_local()`, before processing starts. Processing
    itself never reads the mirror -- `process_one()` builds a `StagedObject`
    straight from the S3 key -- so a mirror failure is logged and swallowed
    rather than allowed to block real invoice processing; it's a local
    convenience copy, not a dependency. Pass `mirror_local=False` to skip
    this entirely, which matters at real production volume: mirroring every
    file on every run would otherwise grow `data/raw` without bound,
    duplicating what S3 already holds durably.
    """
    deps = deps or Dependencies.build()
    batch_id = batch_id or new_batch_id()
    resolved_date = resolve_batch_date(batch_date)
    files = list(discover_s3_files(bucket, resolved_date, client=s3_client))

    if mirror_local:
        try:
            mirror_to_local(files, resolved_date, local_root=local_root, client=s3_client)
        except Exception as exc:  # defensive: a mirror failure must not block processing
            log.warning("local mirror failed, continuing without it: %s", exc)

    return _run_over(files, deps, batch_id, limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the invoice batch pipeline.")
    parser.add_argument("--raw-dir", default=None, help="defaults to paths.raw")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--from-s3",
        action="store_true",
        help="discover from a date-partitioned S3 prefix instead of --raw-dir",
    )
    parser.add_argument(
        "--bucket", default=None, help="--from-s3 only; overrides aws.s3_raw_bucket"
    )
    parser.add_argument(
        "--batch-date",
        default=None,
        help="--from-s3 only; YYYY-MM-DD, overrides pipeline.batch_date_override and today()",
    )
    parser.add_argument(
        "--no-mirror",
        dest="mirror_local",
        action="store_false",
        default=True,
        help="--from-s3 only; skip copying discovered files to data/raw/<y>/<m>/<d>/ locally",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    deps = Dependencies.build()
    if args.from_s3:
        result = run_s3_batch(
            bucket=args.bucket,
            batch_date=args.batch_date,
            deps=deps,
            batch_id=args.batch_id,
            limit=args.limit,
            mirror_local=args.mirror_local,
        )
    else:
        result = run_batch(
            raw_dir=args.raw_dir, deps=deps, batch_id=args.batch_id, limit=args.limit
        )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    # Quarantined files are an expected outcome, not a run failure; a non-zero
    # exit is reserved for a batch that processed nothing at all.
    return 0 if result.outcomes else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
