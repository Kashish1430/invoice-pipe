"""The `InvoiceRecord` Pydantic V2 model -- the pipeline's typed contract.

Ordering inside the model is deliberate and load-bearing (see docs/plan.md S2):

    field_validators (coerce)  ->  domain >= 0 check  ->  FX conversion
    ->  soft DQ flags

Conversion needs typed `Decimal`s, so it cannot run before coercion; the domain
check runs before conversion so bad data fails fast without FX math being done
on it; the zero-value warnings run last so they describe the post-conversion
values that are actually stored and queried downstream.

Money is `Decimal` throughout, never `float`: binary floats round money wrong,
and boto3's DynamoDB serializer rejects `float` outright -- one decision serving
both correctness and the storage layer.
"""

from __future__ import annotations

import re
from datetime import date as date_, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.validation import dq_rules, fx

#: Vendor systems emit these as literal strings where a value should be.
#: Sample 4 prints `GST NO : null` -- the word, not an absent field.
_NULLISH = {"null", "none", "nan", "n/a", "na", "-", "--", ""}

#: Both the canonical money fields and their preserved originals pass through
#: the same coercion, so `original_x / fx_rate_applied == x` stays checkable.
_CANONICAL_MONEY_FIELDS = (
    "product_amount",
    "total_amount",
    "vat_tax_amount",
    "coupon_discount_amount",
)
_MONEY_FIELDS = _CANONICAL_MONEY_FIELDS + tuple(
    f"original_{f}" for f in _CANONICAL_MONEY_FIELDS
)

#: Source date formats seen across vendors. Day-first orders come first: all
#: current samples are Indian, where dd/mm is the convention. A date matching
#: both dd/mm and mm/dd is separately flagged AMBIGUOUS_DATE_FORMAT.
_DATE_FORMATS = (
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%d.%m.%Y",
    "%m/%d/%Y",
)

_CURRENCY_SYMBOLS = re.compile(r"[₹`£$€]|(?i:Rs\.?|INR|GBP|USD|EUR)")
_KERNING_SPLIT = re.compile(r"(?<=\d)\s+(?=\d)")


class InvoiceRecord(BaseModel):
    """One invoice, canonicalised to the 13-field ontology plus provenance."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    # --- 13 canonical fields -------------------------------------------------
    date: date_ = Field(...)
    company_name: str = Field(..., min_length=1)
    buyer_name: str = Field(..., min_length=1)
    invoice_reference_number: str = Field(..., min_length=1)
    product_name: str = Field(default="N/A")
    product_amount: Decimal = Field(default=Decimal("0"))
    total_amount: Decimal = Field(default=Decimal("0"))
    vat_tax_label: str = Field(default="N/A")
    vat_tax_percentage: str = Field(default="N/A")
    vat_tax_amount: Decimal = Field(default=Decimal("0"))
    coupon_discount_amount: Decimal = Field(default=Decimal("0"))
    mode_of_payment: str = Field(default="N/A")
    currency: str = Field(..., min_length=1)  # always "GBP" post-normalization

    # --- provenance / audit (required by design, not part of the "13") -------
    original_currency: str
    original_total_amount: Decimal = Field(default=Decimal("0"))
    original_product_amount: Decimal = Field(default=Decimal("0"))
    original_vat_tax_amount: Decimal = Field(default=Decimal("0"))
    original_coupon_discount_amount: Decimal = Field(default=Decimal("0"))
    fx_rate_applied: Decimal
    fx_rate_date: date_
    source_file: str
    extraction_confidence: dict[str, float] = Field(default_factory=dict)
    dq_flags: list[str] = Field(default_factory=list)
    unmapped_metadata: dict[str, str] = Field(default_factory=dict)

    # ---- validator 1: money coercion ---------------------------------------
    @field_validator(*_MONEY_FIELDS, mode="before")
    @classmethod
    def _coerce_money(cls, v: Any) -> Any:
        """Strip currency noise, rejoin kerning-split digits, then Decimal().

        Sample 1's text layer renders `1,333.00` as `1 1 1,333.00` because the
        PDF kerns individual glyphs; the whitespace between digits is a
        rendering artefact, never a thousands separator.
        """
        if v is None or v == "":
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        s = str(v).strip()
        # Accounting parentheses mean negative; preserve the sign so the domain
        # check below can reject it rather than silently taking a magnitude.
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        s = _CURRENCY_SYMBOLS.sub("", s)
        s = _KERNING_SPLIT.sub("", s)  # "1 1 1,333.00" -> "111,333.00"
        s = s.replace(",", "").strip()
        if s.lower() in _NULLISH:
            return Decimal("0")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"cannot coerce {v!r} to Decimal")
        return -d if negative else d

    # ---- validator 2: date coercion ----------------------------------------
    @field_validator("date", "fx_rate_date", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date_):
            return v
        s = str(v).strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"unrecognized date format: {v!r}")

    # ---- validator 3: literal "null"-string vendor bug (sample 4) -----------
    @field_validator(
        "vat_tax_label",
        "vat_tax_percentage",
        "product_name",
        "mode_of_payment",
        mode="before",
    )
    @classmethod
    def _normalize_nullish_optional_str(cls, v: Any) -> str:
        s = "" if v is None else str(v).strip()
        return "N/A" if s.lower() in _NULLISH else s

    @field_validator(
        "company_name",
        "buyer_name",
        "invoice_reference_number",
        "currency",
        "original_currency",
        mode="before",
    )
    @classmethod
    def _reject_nullish_mandatory_str(cls, v: Any) -> str:
        s = "" if v is None else str(v).strip()
        if s.lower() in _NULLISH:
            raise ValueError(f"mandatory field received nullish literal value {v!r}")
        return s

    @field_validator("currency", "original_currency", mode="after")
    @classmethod
    def _uppercase_currency(cls, v: str) -> str:
        return v.upper()

    # ---- model_validator(after): domain -> FX -> soft DQ flags --------------
    @model_validator(mode="after")
    def _apply_domain_fx_and_dq(self) -> "InvoiceRecord":
        for f in _CANONICAL_MONEY_FIELDS:
            if getattr(self, f) < 0:
                raise ValueError(
                    f"{f}={getattr(self, f)} violates >=0 domain constraint"
                )

        if self.currency != fx.TARGET_CURRENCY:
            rate = self.fx_rate_applied
            if rate <= 0:
                raise ValueError(f"fx_rate_applied={rate} must be positive")
            for f in _CANONICAL_MONEY_FIELDS:
                self._assign(f, fx.to_gbp(getattr(self, f), rate))
            self._assign("currency", fx.TARGET_CURRENCY)

        self._assign(
            "dq_flags",
            dq_rules.merge_flags(self.dq_flags, dq_rules.evaluate_model_local(self)),
        )
        return self

    # ------------------------------------------------------------------------
    def _assign(self, name: str, value: Any) -> None:
        """Write a field from inside the after-validator.

        `validate_assignment=True` is kept because downstream code mutates
        records (the orchestrator appends contextual dq_flags), but a plain
        `setattr` here would re-run this same validator and recurse forever.
        Writing through `__dict__` applies the already-validated value once.
        """
        self.__dict__[name] = value
        self.__pydantic_fields_set__.add(name)

    # ---- convenience used by the storage + stats layers ---------------------
    @property
    def status(self) -> str:
        return dq_rules.status_for(self.dq_flags)

    def add_flags(self, flags: list[str]) -> None:
        """Append contextual DQ flags after construction, without duplicates."""
        self.dq_flags = dq_rules.merge_flags(self.dq_flags, flags)

    def fx_is_reversible(self, tolerance: Decimal = Decimal("0.01")) -> bool:
        """Audit check: every canonical amount reproduces from its original."""
        for f in _CANONICAL_MONEY_FIELDS:
            expected = fx.to_gbp(getattr(self, f"original_{f}"), self.fx_rate_applied)
            if abs(expected - getattr(self, f)) > tolerance:
                return False
        return True
