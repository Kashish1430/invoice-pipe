"""FX table loading, rounding and reversibility."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.validation import fx


def test_rate_lookup_is_case_insensitive(pipeline_env):
    assert fx.rate_for("inr") == fx.rate_for("INR") == Decimal("125")


def test_gbp_rate_is_identity(pipeline_env):
    assert fx.rate_for("GBP") == Decimal("1")


def test_unknown_currency_raises(pipeline_env):
    with pytest.raises(fx.UnknownCurrencyError, match="no FX rate"):
        fx.rate_for("JPY")


def test_empty_currency_raises(pipeline_env):
    with pytest.raises(fx.UnknownCurrencyError):
        fx.rate_for("")


def test_rate_is_decimal_not_float(pipeline_env):
    """A YAML float would reintroduce exactly the rounding Decimal exists to avoid stale results."""
    assert isinstance(fx.rate_for("USD"), Decimal)
    assert fx.rate_for("USD") == Decimal("1.25")


def test_rate_date_comes_from_the_table(pipeline_env):
    assert fx.rate_date() == date(2026, 9, 9)


@pytest.mark.parametrize(
    "amount,expected",
    [
        (Decimal("6000.00"), Decimal("48.00")),
        (Decimal("805.82"), Decimal("6.45")),   # 6.44656 -> half-up by rounding
        (Decimal("0.00"), Decimal("0.00")),
        (Decimal("1.245"), Decimal("0.01")),
    ],
)
def test_to_gbp_rounds_half_up_to_two_places(pipeline_env, amount, expected):
    assert fx.to_gbp(amount, Decimal("125")) == expected


def test_to_gbp_rejects_non_positive_rate(pipeline_env):
    with pytest.raises(fx.UnknownCurrencyError):
        fx.to_gbp(Decimal("10"), Decimal("0"))


def test_conversion_context_supplies_both_provenance_fields(pipeline_env):
    ctx = fx.conversion_context("INR")
    assert ctx == {"fx_rate_applied": Decimal("125"), "fx_rate_date": date(2026, 9, 9)}
