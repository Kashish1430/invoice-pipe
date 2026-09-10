"""The Stage 3 rule table and its evaluators."""

from __future__ import annotations

import pytest

from src.validation import dq_rules
from src.validation.models import InvoiceRecord


# ---------------------------------------------------------------------------
# the declarative table itself
def test_every_documented_rule_is_declared():
    expected = {
        "MISSING_MANDATORY_FIELD",
        "NEGATIVE_MONEY_VALUE",
        "UNPARSEABLE_DATE",
        "UNPARSEABLE_DECIMAL",
        "EXTRACTION_FAILED",
        "ZERO_TOTAL_AMOUNT",
        "ZERO_PRODUCT_AMOUNT",
        "ZERO_TAX_WITH_NONZERO_RATE",
        "LOW_FIELD_CONFIDENCE",
        "AMBIGUOUS_DATE_FORMAT",
        "MULTIPLE_TOTAL_CANDIDATES",
        "DUPLICATE_SKIPPED",
    }
    assert set(dq_rules.BY_NAME) == expected


def test_rule_names_are_unique():
    names = [r.name for r in dq_rules.RULES]
    assert len(names) == len(set(names))


def test_duplicate_skipped_is_not_a_hard_fail():
    """A duplicate is a valid record already loaded, not a failure."""
    assert dq_rules.BY_NAME["DUPLICATE_SKIPPED"].severity == "special"
    assert "DUPLICATE_SKIPPED" not in dq_rules.HARD_FAIL_RULES


def test_model_local_rules_are_a_subset_of_soft_warns():
    assert set(dq_rules.MODEL_LOCAL_RULES) <= set(dq_rules.SOFT_WARN_RULES)


# rate parsing
@pytest.mark.parametrize(
    "percentage,expected",
    [
        ("9%+9%", True),
        ("18%", True),
        ("3%", True),
        ("0%", False),
        ("0%+0%", False),
        ("N/A", False),
        ("", False),
        (None, False),
    ],
)
def test_rate_is_nonzero(percentage, expected):
    assert dq_rules.rate_is_nonzero(percentage) is expected

# date ambiguity
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("03/04/2025", True),    # both components <= 12
        ("12-11-2025", True),
        ("22-05-2022", False),   # day > 12, unambiguous
        ("2024-01-22", False),   # ISO, unambiguous
        ("12 Apr 2026", False),  # month named, unambiguous
        (None, False),
    ],
)
def test_date_is_ambiguous(raw, expected):
    assert dq_rules.date_is_ambiguous(raw) is expected


# confidence
def test_low_confidence_fields_uses_the_configured_threshold(pipeline_env):
    conf = {"buyer_name": 41.2, "total_amount": 97.3, "date": 59.9}
    assert dq_rules.low_confidence_fields(conf) == ["buyer_name", "date"]


def test_low_confidence_threshold_is_overridable():
    conf = {"buyer_name": 41.2, "total_amount": 97.3}
    assert dq_rules.low_confidence_fields(conf, threshold=30.0) == []


def test_empty_confidence_map_flags_nothing():
    assert dq_rules.low_confidence_fields({}) == []
    assert dq_rules.low_confidence_fields(None) == []


# evaluators
def test_contextual_evaluator_covers_the_three_out_of_model_rules(
    record_kwargs, pipeline_env
):
    record = InvoiceRecord(
        **record_kwargs(extraction_confidence={"buyer_name": 41.2})
    )
    flags = dq_rules.evaluate_contextual(
        record, raw_date="03/04/2025", multiple_total_candidates=True
    )
    assert flags == [
        "LOW_FIELD_CONFIDENCE",
        "AMBIGUOUS_DATE_FORMAT",
        "MULTIPLE_TOTAL_CANDIDATES",
    ]


def test_contextual_evaluator_is_quiet_on_a_clean_record(record_kwargs, pipeline_env):
    record = InvoiceRecord(**record_kwargs(extraction_confidence={"total_amount": 98.0}))
    assert dq_rules.evaluate_contextual(record, raw_date="22-05-2022") == []


def test_merge_flags_dedupes_and_preserves_order():
    assert dq_rules.merge_flags(["A", "B"], ["B", "C"]) == ["A", "B", "C"]


def test_status_for():
    assert dq_rules.status_for([]) == "LOADED"
    assert dq_rules.status_for(["ZERO_TOTAL_AMOUNT"]) == "LOADED_WITH_WARNINGS"
