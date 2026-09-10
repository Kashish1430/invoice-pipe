"""Validator tests, driven by what the six real samples actually contain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.validation.models import InvoiceRecord


# money coercion
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("6,000.00", Decimal("6000.00")),
        ("Rs. 1,333.00", Decimal("1333.00")),
        ("INR 805.82", Decimal("805.82")),
        ("£48.00", Decimal("48.00")),
        ("1 1 1,333.00", Decimal("111333.00")),  # sample 1: kerning-split digits
        ("", Decimal("0")),
        ("null", Decimal("0")),  # sample 4: the vendor prints the word
        ("N/A", Decimal("0")),
        ("-", Decimal("0")),
        (None, Decimal("0")),
        (Decimal("12.34"), Decimal("12.34")),
        (5, Decimal("5")),
    ],
)
def test_money_coercion(record_kwargs, pipeline_env, raw, expected):
    record = InvoiceRecord(
        **record_kwargs(
            currency="GBP",
            original_currency="GBP",
            fx_rate_applied=Decimal("1"),
            product_amount=raw,
            original_product_amount=raw,
        )
    )
    assert record.product_amount == expected


def test_money_is_decimal_never_float(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs())
    for field in (
        "product_amount",
        "total_amount",
        "vat_tax_amount",
        "coupon_discount_amount",
    ):
        assert isinstance(getattr(record, field), Decimal)


def test_unparseable_money_is_a_hard_failure(record_kwargs, pipeline_env):
    with pytest.raises(ValidationError, match="cannot coerce"):
        InvoiceRecord(**record_kwargs(total_amount="six thousand"))


def test_zero_is_a_valid_business_value_not_a_missing_one(record_kwargs, pipeline_env):
    """Sample 1's `Grand Total: 0.00` is legitimate, so 0 must load."""
    record = InvoiceRecord(**record_kwargs(total_amount="0.00", original_total_amount="0.00"))
    assert record.total_amount == Decimal("0")
    assert "ZERO_TOTAL_AMOUNT" in record.dq_flags


# date coercion
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("22-05-2022", date(2022, 5, 22)),
        ("18/07/2025", date(2025, 7, 18)),
        ("2024-01-22", date(2024, 1, 22)),
        ("12 Apr 2026", date(2026, 4, 12)),
        ("12 April 2026", date(2026, 4, 12)),
        ("23-Jun-2025", date(2025, 6, 23)),
        ("23.06.2025", date(2025, 6, 23)),
        ("October 6, 2025", date(2025, 10, 6)),  # real: E-Receipt (2)'s Trip.com date
    ],
)
def test_date_coercion(record_kwargs, pipeline_env, raw, expected):
    assert InvoiceRecord(**record_kwargs(date=raw)).date == expected


def test_unparseable_date_is_a_hard_failure(record_kwargs, pipeline_env):
    with pytest.raises(ValidationError, match="unrecognized date format"):
        InvoiceRecord(**record_kwargs(date="last Tuesday"))


def test_day_first_wins_for_ambiguous_numeric_dates(record_kwargs, pipeline_env):
    """Given that All current vendors are Indian, so dd/mm is tried before mm/dd."""
    assert InvoiceRecord(**record_kwargs(date="03/04/2025")).date == date(2025, 4, 3)


# nullish strings (sample 4: `GST NO : null`)
@pytest.mark.parametrize("raw", ["null", "None", "NaN", "n/a", "NA", "", "  ", None])
def test_nullish_optional_strings_become_na(record_kwargs, pipeline_env, raw):
    record = InvoiceRecord(**record_kwargs(mode_of_payment=raw, product_name=raw))
    assert record.mode_of_payment == "N/A"
    assert record.product_name == "N/A"


@pytest.mark.parametrize(
    "field", ["company_name", "buyer_name", "invoice_reference_number", "currency"]
)
def test_nullish_mandatory_strings_are_rejected(record_kwargs, pipeline_env, field):
    with pytest.raises(ValidationError, match="nullish literal"):
        InvoiceRecord(**record_kwargs(**{field: "null"}))


def test_garbled_buyer_name_is_preserved_not_rejected(record_kwargs, pipeline_env):
    """Sample 1's corrupted cmap: unreadable is still a value, not a failure."""
    garbled = "' ( 9 . ,  + , 7 ( 6 +  1 $  7 +"
    assert InvoiceRecord(**record_kwargs(buyer_name=garbled)).buyer_name == garbled


# domain assertion
def test_negative_money_is_rejected(record_kwargs, pipeline_env):
    with pytest.raises(ValidationError, match="violates >=0 domain constraint"):
        InvoiceRecord(**record_kwargs(total_amount="-1,250.00"))


def test_parenthesised_amount_is_treated_as_negative(record_kwargs, pipeline_env):
    with pytest.raises(ValidationError, match="violates >=0 domain constraint"):
        InvoiceRecord(**record_kwargs(total_amount="(1250.00)"))


# FX conversion, ordering and provenance
def test_inr_is_converted_and_currency_normalised(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs())
    assert record.currency == "GBP"
    assert record.original_currency == "INR"
    assert record.total_amount == Decimal("48.00")  # 6000 / 125
    assert record.original_total_amount == Decimal("6000.00")


def test_conversion_is_reversible(record_kwargs, pipeline_env):
    assert InvoiceRecord(**record_kwargs()).fx_is_reversible()


def test_gbp_records_are_not_reconverted(record_kwargs, pipeline_env):
    record = InvoiceRecord(
        **record_kwargs(
            currency="GBP", original_currency="GBP", fx_rate_applied=Decimal("1")
        )
    )
    assert record.total_amount == Decimal("6000.00")


def test_domain_check_runs_before_fx(record_kwargs, pipeline_env):
    """A negative value must fail on its own terms as we don't have refund invoices,
     not after FX(conversion) math."""
    with pytest.raises(ValidationError) as exc:
        InvoiceRecord(**record_kwargs(total_amount="-6000.00"))
    assert "-6000.00" in str(exc.value)  # the pre-conversion figure


def test_dq_flags_describe_post_conversion_values(record_kwargs, pipeline_env):
    """A sub-penny INR amount rounds to 0 GBP, and the flag reflects what is stored."""
    record = InvoiceRecord(
        **record_kwargs(total_amount="0.50", original_total_amount="0.50")
    )
    assert record.total_amount == Decimal("0.00")
    assert "ZERO_TOTAL_AMOUNT" in record.dq_flags


def test_unknown_currency_has_no_rate(record_kwargs, pipeline_env):
    from src.validation.fx import UnknownCurrencyError, rate_for

    with pytest.raises(UnknownCurrencyError):
        rate_for("JPY")


# soft DQ flags
def test_zero_tax_with_nonzero_rate_flag(record_kwargs, pipeline_env):
    """Sample 4: SGST/CGST both print 9% against a 0.00 amount."""
    record = InvoiceRecord(**record_kwargs())
    assert "ZERO_TAX_WITH_NONZERO_RATE" in record.dq_flags
    assert record.status == "LOADED_WITH_WARNINGS"


def test_zero_tax_with_zero_rate_is_not_flagged(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs(vat_tax_percentage="0%"))
    assert "ZERO_TAX_WITH_NONZERO_RATE" not in record.dq_flags


def test_clean_record_has_no_flags(record_kwargs, pipeline_env):
    record = InvoiceRecord(
        **record_kwargs(
            vat_tax_percentage="18%",
            vat_tax_amount="216.00",
            original_vat_tax_amount="216.00",
        )
    )
    assert record.dq_flags == []
    assert record.status == "LOADED"


def test_flags_are_not_duplicated_on_reassignment(record_kwargs, pipeline_env):
    """`validate_assignment` re-runs the model validator; flags must not double."""
    record = InvoiceRecord(**record_kwargs())
    record.mode_of_payment = "Card"
    assert record.dq_flags.count("ZERO_TAX_WITH_NONZERO_RATE") == 1


def test_add_flags_is_idempotent(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs())
    record.add_flags(["LOW_FIELD_CONFIDENCE"])
    record.add_flags(["LOW_FIELD_CONFIDENCE"])
    assert record.dq_flags.count("LOW_FIELD_CONFIDENCE") == 1


def test_address_is_never_a_top_level_field(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs(unmapped_metadata={"address": "Ahmedabad"}))
    assert "address" not in record.model_dump()
    assert record.unmapped_metadata["address"] == "Ahmedabad"
