"""Writes `data/quarantine/<filename>.error.json` for hard-failed documents.

`data/raw` is immutable: the PDF is never moved, renamed or deleted on failure.
A sidecar keeps the evidence and the diagnosis side by side without disturbing
the input, so re-running a fixed batch means re-running the same folder.

Pydantic V2's `ValidationError.errors()` is already a structured list of
`{loc, msg, type, input}`, so it is written through as-is rather than reshaped
into a bespoke error format.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src import settings
from src.pipeline.errors import PipelineError


def _error_detail(exc: BaseException) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return exc.errors()
    return [{"loc": [], "msg": str(exc), "type": type(exc).__name__, "input": None}]


def _stage_of(exc: BaseException, fallback: str) -> str:
    if isinstance(exc, ValidationError):
        return "pydantic_validation"
    return getattr(exc, "stage", None) or fallback


def dq_rule_for(exc: BaseException) -> str:
    """Which Stage-3 hard-fail rule this exception represents."""
    if isinstance(exc, PipelineError):
        return exc.dq_rule
    if isinstance(exc, ValidationError):
        # Map the first Pydantic error to the rule that describes it. The rule
        # names are what the stats job and the tombstones group on, so a
        # ValidationError has to resolve to one of them rather than to its own
        # Pydantic error type.
        for err in exc.errors():
            msg = str(err.get("msg", "")).lower()
            etype = str(err.get("type", ""))
            if "nullish literal" in msg or etype == "missing":
                return "MISSING_MANDATORY_FIELD"
            if "domain constraint" in msg:
                return "NEGATIVE_MONEY_VALUE"
            if "unrecognized date format" in msg:
                return "UNPARSEABLE_DATE"
            if "cannot coerce" in msg:
                return "UNPARSEABLE_DECIMAL"
        return "MISSING_MANDATORY_FIELD"
    return "EXTRACTION_FAILED"


def build_sidecar(
    source_path: str | Path,
    exc: BaseException,
    batch_id: str,
    *,
    raw_mapped_payload: dict[str, Any] | None = None,
    error_type: str | None = None,
    failed_at_stage: str = "unknown",
) -> dict[str, Any]:
    return {
        "source_file": str(source_path).replace("\\", "/"),
        "failed_at_stage": _stage_of(exc, failed_at_stage),
        "failed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error_type": error_type or type(exc).__name__,
        "dq_rule": dq_rule_for(exc),
        "error_detail": _error_detail(exc),
        "raw_mapped_payload": raw_mapped_payload or {},
        "batch_id": batch_id,
    }


def write_quarantine_sidecar(
    source_path: str | Path,
    exc: BaseException,
    batch_id: str,
    *,
    raw_mapped_payload: dict[str, Any] | None = None,
    error_type: str | None = None,
    failed_at_stage: str = "unknown",
    quarantine_dir: str | Path | None = None,
) -> Path:
    """Persist the sidecar and return where it landed."""
    directory = (
        Path(quarantine_dir)
        if quarantine_dir is not None
        else settings.data_path("quarantine")
    )
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{Path(source_path).name}.error.json"

    payload = build_sidecar(
        source_path,
        exc,
        batch_id,
        raw_mapped_payload=raw_mapped_payload,
        error_type=error_type,
        failed_at_stage=failed_at_stage,
    )
    with open(out, "w", encoding="utf-8") as fh:
        # default=str: a ValidationError's `input` can hold a Decimal, a date or
        # any raw value that failed coercion. Losing the sidecar to a
        # serialisation error would destroy the only record of the failure.
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return out
