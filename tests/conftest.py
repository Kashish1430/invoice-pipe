"""Shared test fixtures.

Every test runs against a settings file generated into `tmp_path`, so no test
reads the developer's `config/` or writes into the real `data/` tree. The YAML
loaders are cached, so each fixture resets those caches after repointing the
environment variables -- otherwise the first test to load settings would pin
them for the whole session.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from src import settings
from src.extraction.raw_shapes import (
    from_document_analysis_response,
    from_expense_response,
)
from src.mapping import field_semantics
from src.mapping.bedrock_client import MappedPayload
from src.pipeline.errors import BedrockMappingError, TextractExtractionError
from src.validation import fx

FIXTURES = Path(__file__).parent / "fixtures"
TEXTRACT_RESPONSES = FIXTURES / "textract_responses"
MAPPED = FIXTURES / "mapped"


class FixtureTextractClient:
    """Test double with `TextractClient`'s public interface (`.analyze()`),
    routing to the real Textract-response fixture for the file being asked
    about.

    Exercises the real parsers (`from_expense_response` /
    `from_document_analysis_response`) rather than bypassing them. AWS call
    mechanics -- polling, pagination, the FORMS+TABLES fallback trigger
    are already covered directly against `FakeTextract` in
    `test_textract_mock.py`; orchestration tests care about batch-level
    behavior (per-file isolation, quarantine, dedup), not how the
    extraction call itself is made, so re-proving that mechanics here would
    just be duplicate coverage.
    """

    def __init__(self, responses_by_stem: dict[str, dict]):
        self._responses = responses_by_stem

    def analyze(self, staged, source_file):
        stem = Path(source_file).stem
        response = self._responses.get(stem)
        if response is None:
            raise TextractExtractionError(
                f"{source_file}: no fixture registered for this file"
            )
        if "Blocks" in response:
            return from_document_analysis_response(response, source_file)
        return from_expense_response(response, source_file)


class FixtureBedrockMapper:
    """Same idea as `FixtureTextractClient`, for the mapping leg."""

    def __init__(self, mapped_by_stem: dict[str, dict]):
        self._mapped = mapped_by_stem

    def map_document(self, doc):
        stem = Path(doc.source_file).stem
        payload = self._mapped.get(stem)
        if payload is None:
            raise BedrockMappingError(
                f"{doc.source_file}: no fixture registered for this file"
            )
        return MappedPayload.from_dict(payload)


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    """Point every config path and data path at an isolated temp tree."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    settings_file = config_dir / "settings.yaml"
    settings_file.write_text(
        yaml.safe_dump(
            {
                "aws": {
                    "region": "eu-west-2",
                    "s3_bucket": "test-bucket",
                    "s3_incoming_prefix": "incoming",
                },
                "textract": {
                    "low_confidence_threshold": 60.0,
                    "fallback_confidence_threshold": 50.0,
                    "poll_interval_seconds": 0.0,
                    "poll_timeout_seconds": 5.0,
                },
                "bedrock": {
                    "model_id": "test-haiku",
                    "escalation_model_id": "test-sonnet",
                    "max_tokens": 1024,
                    "temperature": 0.0,
                    "max_retries": 1,
                },
                "storage": {"table_name": "invoice_records_test", "schema_version": 1},
                "paths": {
                    "raw": str(tmp_path / "data" / "raw"),
                    "silver": str(tmp_path / "data" / "silver"),
                    "gold": str(tmp_path / "data" / "gold"),
                    "quarantine": str(tmp_path / "data" / "quarantine"),
                },
                "pipeline": {},
            }
        ),
        encoding="utf-8",
    )

    fx_file = config_dir / "fx_rates.yaml"
    fx_file.write_text(
        yaml.safe_dump(
            {
                "as_of": "2026-09-09",
                "base": "GBP",
                "rates": {"INR_GBP": 125, "USD_GBP": 1.25, "GBP_GBP": 1},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("INVOICE_PIPE_SETTINGS", str(settings_file))
    monkeypatch.setenv("INVOICE_PIPE_FX_RATES", str(fx_file))
    settings.reset_cache()
    fx.reset_cache()
    field_semantics.reset_cache()

    for kind in ("raw", "silver", "gold", "quarantine"):
        settings.data_path(kind).mkdir(parents=True, exist_ok=True)

    yield tmp_path

    settings.reset_cache()
    fx.reset_cache()
    field_semantics.reset_cache()


@pytest.fixture
def textract_response():
    """Load a raw AWS-shaped fixture by name."""

    def _load(name: str) -> dict:
        with open(TEXTRACT_RESPONSES / f"{name}.json", "r", encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def mapped_fixture():
    """Load the expected Bedrock tool output for one sample."""

    def _load(name: str) -> dict:
        with open(MAPPED / f"{name}.mapped.json", "r", encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def record_kwargs():
    """A minimal valid `InvoiceRecord` payload, overridable per test."""

    def _make(**overrides):
        base = {
            "date": "22-05-2022",
            "company_name": "Gym Lounge",
            "buyer_name": "DEVKI NATH",
            "invoice_reference_number": "Gym Lounge//2022-2023/240",
            "product_name": "Gym Workout",
            "product_amount": "15000.00",
            "total_amount": "6000.00",
            "vat_tax_label": "SGST+CGST",
            "vat_tax_percentage": "9%+9%",
            "vat_tax_amount": "0.00",
            "coupon_discount_amount": "9000.00",
            "mode_of_payment": "Cash",
            "currency": "INR",
            "original_currency": "INR",
            "original_total_amount": "6000.00",
            "original_product_amount": "15000.00",
            "original_vat_tax_amount": "0.00",
            "original_coupon_discount_amount": "9000.00",
            "fx_rate_applied": Decimal("125"),
            "fx_rate_date": "2026-09-09",
            "source_file": "Invoice1653194348.pdf",
        }
        base.update(overrides)
        return base

    return _make
