"""Quarantine sidecars: rule attribution and serialisation robustness."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.pipeline.errors import (
    BedrockMappingError,
    DuplicateRecordError,
    TextractExtractionError,
)
from src.quarantine.sidecar_writer import (
    build_sidecar,
    dq_rule_for,
    write_quarantine_sidecar,
)
from src.validation.models import InvoiceRecord


def validation_error(record_kwargs, **overrides) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        InvoiceRecord(**record_kwargs(**overrides))
    return exc.value


# rule attribution

def test_negative_money_maps_to_its_rule(record_kwargs, pipeline_env):
    exc = validation_error(record_kwargs, total_amount="-1.00")
    assert dq_rule_for(exc) == "NEGATIVE_MONEY_VALUE"


def test_unparseable_date_maps_to_its_rule(record_kwargs, pipeline_env):
    exc = validation_error(record_kwargs, date="last Tuesday")
    assert dq_rule_for(exc) == "UNPARSEABLE_DATE"


def test_unparseable_decimal_maps_to_its_rule(record_kwargs, pipeline_env):
    exc = validation_error(record_kwargs, total_amount="six thousand")
    assert dq_rule_for(exc) == "UNPARSEABLE_DECIMAL"


def test_nullish_mandatory_field_maps_to_its_rule(record_kwargs, pipeline_env):
    exc = validation_error(record_kwargs, company_name="null")
    assert dq_rule_for(exc) == "MISSING_MANDATORY_FIELD"


def test_absent_mandatory_field_maps_to_its_rule(record_kwargs, pipeline_env):
    kwargs = record_kwargs()
    kwargs.pop("company_name")
    with pytest.raises(ValidationError) as exc:
        InvoiceRecord(**kwargs)
    assert dq_rule_for(exc.value) == "MISSING_MANDATORY_FIELD"


@pytest.mark.parametrize(
    "exc", [TextractExtractionError("boom"), BedrockMappingError("boom")]
)
def test_stage_failures_map_to_extraction_failed(exc):
    assert dq_rule_for(exc) == "EXTRACTION_FAILED"


def test_duplicate_is_not_an_extraction_failure():
    assert dq_rule_for(DuplicateRecordError("dupe")) == "DUPLICATE_SKIPPED"


# sidecar contents
def test_sidecar_uses_pydantics_native_error_list(record_kwargs, pipeline_env):
    exc = validation_error(record_kwargs, total_amount="-1.00")
    payload = build_sidecar("data/raw/x.pdf", exc, "b1")
    assert payload["error_detail"] == exc.errors()
    assert payload["failed_at_stage"] == "pydantic_validation"
    assert payload["batch_id"] == "b1"


def test_pipeline_errors_report_their_own_stage():
    payload = build_sidecar("data/raw/x.pdf", BedrockMappingError("boom"), "b1")
    assert payload["failed_at_stage"] == "mapping"
    assert payload["error_type"] == "BedrockMappingError"


def test_sidecar_survives_values_json_cannot_serialise(pipeline_env, tmp_path):
    """A ValidationError's `input` can hold a Decimal or a date; losing the
    sidecar to that would destroy the only record of the failure."""
    exc = ValueError("bad")
    path = write_quarantine_sidecar(
        "data/raw/x.pdf",
        exc,
        "b1",
        raw_mapped_payload={"amount": Decimal("1.23")},
        quarantine_dir=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["raw_mapped_payload"]["amount"] == "1.23"


def test_sidecar_is_named_after_the_source_file(pipeline_env, tmp_path):
    path = write_quarantine_sidecar(
        "data/raw/SALES RECEIPT_304743.pdf",
        ValueError("bad"),
        "b1",
        quarantine_dir=tmp_path,
    )
    assert path.name == "SALES RECEIPT_304743.pdf.error.json"
