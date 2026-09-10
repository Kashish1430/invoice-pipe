"""Stage 3 data-quality rule table and evaluators.

The rule set from docs/plan.md is declared here as data, once, so the same names
drive the Pydantic layer, the quarantine sidecars, the DynamoDB `status`
attribute and the batch stats job. Nothing else in the codebase should invent a
flag string.

Two kinds of soft rule exist, split by what they can see:

* *model-local* rules depend only on the record's own typed fields, so
  `InvoiceRecord`'s model_validator evaluates them at construction time;
* *contextual* rules need something outside the model (Textract confidences, the
  date string before coercion, the mapper's own disambiguation signal), so the
  orchestrator evaluates them after construction.

This module deliberately does not import `models` -- it duck-types the record --
so `models` can import it without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Literal

from src import settings

Severity = Literal["hard_fail", "soft_warn", "special"]


@dataclass(frozen=True)
class Rule:
    name: str
    severity: Severity
    stage: str
    condition: str
    action: str
    example: str | None = None


RULES: tuple[Rule, ...] = (
    # ---- hard fails: quarantine, never reach storage ------------------------
    Rule(
        name="MISSING_MANDATORY_FIELD",
        severity="hard_fail",
        stage="pydantic_validation",
        condition=(
            "date | company_name | buyer_name | invoice_reference_number | "
            "currency is empty/nullish"
        ),
        action="quarantine",
    ),
    Rule(
        name="NEGATIVE_MONEY_VALUE",
        severity="hard_fail",
        stage="pydantic_validation",
        condition="any money field < 0",
        action="quarantine",
    ),
    Rule(
        name="UNPARSEABLE_DATE",
        severity="hard_fail",
        stage="pydantic_validation",
        condition="date string matches none of the known formats",
        action="quarantine",
    ),
    Rule(
        name="UNPARSEABLE_DECIMAL",
        severity="hard_fail",
        stage="pydantic_validation",
        condition="money string cannot coerce to Decimal after cleaning",
        action="quarantine",
    ),
    Rule(
        name="EXTRACTION_FAILED",
        severity="hard_fail",
        stage="extraction_or_mapping",
        condition=(
            "Textract job failed / Bedrock tool-use call errored or returned "
            "invalid schema after retry"
        ),
        action="quarantine",
    ),
    # ---- soft warns: load with dq_flags, no quarantine ----------------------
    Rule(
        name="ZERO_TOTAL_AMOUNT",
        severity="soft_warn",
        stage="pydantic_validation",
        condition="total_amount == 0",
        action="load_with_warning",
        example="2324GBRAMD125920.pdf (VFS Grand Total legitimately 0.00)",
    ),
    Rule(
        name="ZERO_PRODUCT_AMOUNT",
        severity="soft_warn",
        stage="pydantic_validation",
        condition="product_amount == 0",
        action="load_with_warning",
    ),
    Rule(
        name="ZERO_TAX_WITH_NONZERO_RATE",
        severity="soft_warn",
        stage="pydantic_validation",
        condition="vat_tax_amount == 0 and vat_tax_percentage denotes a non-zero rate",
        action="load_with_warning",
        example="Invoice1653194348.pdf (SGST/CGST 9% but amount 0.00)",
    ),
    Rule(
        name="LOW_FIELD_CONFIDENCE",
        severity="soft_warn",
        stage="post_validation",
        condition="any successfully-typed field's Textract confidence < threshold",
        action="load_with_warning",
    ),
    Rule(
        name="AMBIGUOUS_DATE_FORMAT",
        severity="soft_warn",
        stage="post_validation",
        condition="numeric date where day and month are both <= 12 (dd/mm vs mm/dd)",
        action="load_with_warning",
    ),
    Rule(
        name="MULTIPLE_TOTAL_CANDIDATES",
        severity="soft_warn",
        stage="post_validation",
        condition="mapper selected among >= 2 plausible 'total'-labeled source fields",
        action="load_with_warning",
        example="7042968270_543523577_4_2026.pdf (4+ competing totals)",
    ),
    # ---- special routing ----------------------------------------------------
    Rule(
        name="DUPLICATE_SKIPPED",
        severity="special",
        stage="storage",
        condition="conditional write fails because the natural key already exists",
        action=(
            "log as duplicate outcome; NOT quarantine -- the record is valid, "
            "just already loaded"
        ),
    ),
)

BY_NAME: dict[str, Rule] = {r.name: r for r in RULES}
HARD_FAIL_RULES: tuple[str, ...] = tuple(
    r.name for r in RULES if r.severity == "hard_fail"
)
SOFT_WARN_RULES: tuple[str, ...] = tuple(
    r.name for r in RULES if r.severity == "soft_warn"
)

#: Model-local subset, evaluated inside InvoiceRecord itself.
MODEL_LOCAL_RULES: tuple[str, ...] = (
    "ZERO_TOTAL_AMOUNT",
    "ZERO_PRODUCT_AMOUNT",
    "ZERO_TAX_WITH_NONZERO_RATE",
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})\s*$")


def rate_is_nonzero(vat_tax_percentage: Any) -> bool:
    """True when the printed rate label denotes an actual non-zero rate.

    Handles the multi-component Indian case ("9%+9%") and the placeholder cases
    ("N/A", "0%", "") without turning the field into a number -- it stays a
    label so composite rates survive intact.
    """
    s = "" if vat_tax_percentage is None else str(vat_tax_percentage).strip()
    if not s or s.upper() == "N/A":
        return False
    return any(Decimal(m) > 0 for m in _NUMBER.findall(s))


def date_is_ambiguous(raw_date: Any) -> bool:
    """dd/mm vs mm/dd cannot be told apart when both components are <= 12.

    Takes the *pre-coercion* string: once Pydantic has produced a `date` the
    ambiguity is gone, silently resolved by whichever format matched first.
    """
    if raw_date is None:
        return False
    m = _NUMERIC_DATE.match(str(raw_date))
    if not m:
        return False
    first, second = int(m.group(1)), int(m.group(2))
    return 1 <= first <= 12 and 1 <= second <= 12


def low_confidence_fields(
    extraction_confidence: dict[str, float] | None,
    threshold: float | None = None,
) -> list[str]:
    """Field names whose Textract confidence fell below the configured floor."""
    if not extraction_confidence:
        return []
    if threshold is None:
        threshold = float(settings.get("textract.low_confidence_threshold", 60.0))
    return sorted(
        f
        for f, c in extraction_confidence.items()
        if c is not None and float(c) < threshold
    )


def evaluate_model_local(record: Any) -> list[str]:
    """Soft flags derivable from the record's own typed, post-FX fields."""
    flags: list[str] = []
    if record.total_amount == 0:
        flags.append("ZERO_TOTAL_AMOUNT")
    if record.product_amount == 0:
        flags.append("ZERO_PRODUCT_AMOUNT")
    if record.vat_tax_amount == 0 and rate_is_nonzero(record.vat_tax_percentage):
        flags.append("ZERO_TAX_WITH_NONZERO_RATE")
    return flags


def evaluate_contextual(
    record: Any,
    *,
    raw_date: Any = None,
    multiple_total_candidates: bool = False,
    threshold: float | None = None,
) -> list[str]:
    """Soft flags needing evidence from outside the model."""
    flags: list[str] = []
    if low_confidence_fields(getattr(record, "extraction_confidence", None), threshold):
        flags.append("LOW_FIELD_CONFIDENCE")
    if date_is_ambiguous(raw_date):
        flags.append("AMBIGUOUS_DATE_FORMAT")
    if multiple_total_candidates:
        flags.append("MULTIPLE_TOTAL_CANDIDATES")
    return flags


def merge_flags(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    """Append without duplicating -- order-stable, so sidecars diff cleanly."""
    out = list(existing)
    for f in new:
        if f not in out:
            out.append(f)
    return out


def status_for(dq_flags: Iterable[str]) -> str:
    """DynamoDB `status` / GSI2 partition for a record that reached storage."""
    return "LOADED_WITH_WARNINGS" if list(dq_flags) else "LOADED"
