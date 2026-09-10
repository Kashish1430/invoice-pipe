"""Storage leg against moto (not boto): item shape, idempotent writes, GSI queries."""

from __future__ import annotations

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.pipeline.errors import DuplicateRecordError
from src.storage.dynamo_client import DynamoStore, build_item, table_definition
from src.validation.models import InvoiceRecord

TABLE = "invoice_records_test"


@pytest.fixture
def store(pipeline_env, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-2")
        resource.create_table(**table_definition(TABLE))
        yield DynamoStore(table_name=TABLE, resource=resource)


@pytest.fixture
def record(record_kwargs, pipeline_env):
    return InvoiceRecord(**record_kwargs())


# item shape
def test_item_carries_the_documented_keys(record):
    item = build_item(record, ingested_at_utc="2026-09-09T12:00:00Z")
    assert item["PK"] == "VENDOR#GYMLOUNGE"
    assert item["SK"] == "INVOICE#GYMLOUNGE20222023240"
    assert item["dedup_key"] == "GYMLOUNGE|GYMLOUNGE20222023240"
    assert item["GSI1PK"] == "DATE#2022-05"
    assert item["GSI1SK"] == "2022-05-22#VENDOR#GYMLOUNGE"
    assert item["GSI2PK"] == "STATUS#LOADED_WITH_WARNINGS"
    assert item["GSI2SK"] == "2026-09-09T12:00:00Z"
    assert item["status"] == "LOADED_WITH_WARNINGS"
    assert item["schema_version"] == 1


def test_human_readable_invoice_number_survives_on_the_item(record):
    """Only the key is folded; the vendor's own spacing stays queryable."""
    item = build_item(record)
    assert item["invoice_reference_number"] == "Gym Lounge//2022-2023/240"


def test_money_reaches_dynamo_as_decimal(record):
    item = build_item(record)
    assert isinstance(item["total_amount"], Decimal)
    assert item["total_amount"] == Decimal("48.00")


def test_no_floats_survive_into_the_item(record_kwargs, pipeline_env):
    """boto3 raises TypeError on float; Textract confidences arrive as floats."""
    record = InvoiceRecord(
        **record_kwargs(extraction_confidence={"total_amount": 98.2, "date": 95.0})
    )
    item = build_item(record)

    def walk(node):
        if isinstance(node, dict):
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return isinstance(node, float)

    assert not walk(item)
    assert item["extraction_confidence"]["total_amount"] == Decimal("98.2")


def test_dates_are_stored_iso_formatted(record):
    item = build_item(record)
    assert item["date"] == "2022-05-22"
    assert item["fx_rate_date"] == "2026-09-09"


# idempotent write, ensuring same results even when same pipeline is ran multiple times.
def test_first_write_lands(store, record):
    store.put_record(record)
    stored = store.get_by_key(record.company_name, record.invoice_reference_number)
    assert stored["total_amount"] == Decimal("48.00")
    assert stored["original_total_amount"] == Decimal("6000.00")


def test_second_write_of_the_same_invoice_is_a_duplicate(store, record):
    store.put_record(record)
    with pytest.raises(DuplicateRecordError, match="already loaded"):
        store.put_record(record)


def test_reprinted_invoice_number_is_recognised_as_the_same_record(
    store, record, record_kwargs, pipeline_env
):
    """The Gym Lounge re-spacing must not create a second partition."""
    store.put_record(record)
    respaced = InvoiceRecord(
        **record_kwargs(invoice_reference_number="Gym Lounge/ / 2022- 2023/ 240")
    )
    with pytest.raises(DuplicateRecordError):
        store.put_record(respaced)


def test_a_different_invoice_from_the_same_vendor_still_loads(
    store, record, record_kwargs, pipeline_env
):
    store.put_record(record)
    other = InvoiceRecord(**record_kwargs(invoice_reference_number="Gym Lounge//2022-2023/241"))
    store.put_record(other)
    assert len(store.scan_all()) == 2



# indexes

def test_month_query_uses_gsi1(store, record, record_kwargs, pipeline_env):
    store.put_record(record)
    store.put_record(
        InvoiceRecord(
            **record_kwargs(date="14-06-2022", invoice_reference_number="GL/2022/999")
        )
    )
    assert len(store.query_month("2022-05")) == 1
    assert len(store.query_month("2022-06")) == 1
    assert store.query_month("2030-01") == []


def test_status_query_uses_gsi2(store, record, record_kwargs, pipeline_env):
    store.put_record(record)  # LOADED_WITH_WARNINGS
    clean = InvoiceRecord(
        **record_kwargs(
            invoice_reference_number="GL/2022/777",
            vat_tax_percentage="18%",
            vat_tax_amount="216.00",
            original_vat_tax_amount="216.00",
        )
    )
    store.put_record(clean)
    assert len(store.query_status("LOADED")) == 1
    assert len(store.query_status("LOADED_WITH_WARNINGS")) == 1


def test_quarantine_tombstone_is_queryable_by_status(store):
    """Quarantined files never reach the table as records; the tombstone is
    what keeps an ops dashboard on one datastore instead of two."""
    store.put_quarantine_tombstone(
        filename="SALES RECEIPT_304743_1750688634308.pdf",
        batch_id="2026-09-09T12-00-00Z-run01",
        error_type="ValidationError",
        failed_at_stage="pydantic_validation",
    )
    rows = store.query_status("QUARANTINED")
    assert len(rows) == 1
    assert rows[0]["error_type"] == "ValidationError"
    assert rows[0]["PK"].startswith("QUARANTINE#")


def test_create_table_if_missing_is_idempotent(pipeline_env, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-2")
        store = DynamoStore(table_name="fresh_table", resource=resource)
        store.create_table_if_missing()
        store.create_table_if_missing()
        assert store.table.table_status == "ACTIVE"
