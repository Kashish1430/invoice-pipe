"""NaturalLanguageQueryAgent: forced-tool interpretation, deterministic
aggregation, phrased answer. Not the full rigor the rest of this project
carries (built under real time pressure) -- covers the core paths: each
operation, each aggregation, the vendor filter, quarantine exclusion, and
the no-tool-call failure mode.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.query.nl_query import NaturalLanguageQueryAgent, TOOL_NAME
from src.pipeline.errors import BedrockMappingError
from src.storage.dynamo_client import DynamoStore, table_definition
from src.validation.models import InvoiceRecord

TABLE = "invoice_records_test"


class FakeBedrock:
    """Queued Converse responses -- one per call the agent makes."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def tool_call(**tool_input):
    return {
        "output": {"message": {"content": [{"toolUse": {"name": TOOL_NAME, "input": tool_input}}]}},
        "stopReason": "tool_use",
    }


def text_answer(text):
    return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": "end_turn"}


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
def seeded_store(store, record_kwargs, pipeline_env):
    """Two loaded records (different vendors, different months) + one
    quarantine tombstone."""
    store.put_record(InvoiceRecord(**record_kwargs()))  # Gym Lounge, 2022-05, £48.00
    store.put_record(InvoiceRecord(**record_kwargs(
        company_name="Coffee Culture",
        buyer_name="Devki Nath",
        invoice_reference_number="7104598035",
        date="25-07-2025",
        total_amount="235.93",
        original_total_amount="235.93",
        currency="GBP",
        original_currency="GBP",
        fx_rate_applied=Decimal("1"),
        vat_tax_percentage="18%",           # avoid ZERO_TAX_WITH_NONZERO_RATE --
        vat_tax_amount="10.00",             # this one should load fully clean
        original_vat_tax_amount="10.00",
    )))
    store.put_quarantine_tombstone(
        filename="bad.pdf", batch_id="b1", error_type="ValidationError",
        failed_at_stage="pydantic_validation",
    )
    return store


def test_count_via_scan_all(seeded_store):
    fake = FakeBedrock(
        tool_call(operation="scan_all", aggregation="count"),
        text_answer("There are 2 invoices."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("how many invoices do we have?")
    assert result.matched == 2  # tombstone excluded
    assert result.answer == "There are 2 invoices."


def test_sum_total_amount_is_computed_in_python_not_by_the_model(seeded_store):
    """The model never sees or states the sum -- it only chooses the
    aggregation; Python does the Decimal arithmetic."""
    fake = FakeBedrock(
        tool_call(operation="scan_all", aggregation="sum_total_amount"),
        text_answer("placeholder"),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("what's the total value of everything?")
    assert result.total_gbp == Decimal("48.00") + Decimal("235.93")


def test_query_status_uses_gsi2(seeded_store):
    fake = FakeBedrock(
        tool_call(operation="query_status", status="LOADED_WITH_WARNINGS", aggregation="count"),
        text_answer("1 record."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("how many invoices loaded with warnings?")
    assert result.matched == 1


def test_query_status_quarantined_includes_the_tombstone(seeded_store):
    """The one case where a tombstone is exactly what was asked for."""
    fake = FakeBedrock(
        tool_call(operation="query_status", status="QUARANTINED", aggregation="count"),
        text_answer("1 quarantined."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("how many were quarantined?")
    assert result.matched == 1


def test_query_month_uses_gsi1(seeded_store):
    fake = FakeBedrock(
        tool_call(operation="query_month", year_month="2022-05", aggregation="count"),
        text_answer("1 in May 2022."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("how many invoices in May 2022?")
    assert result.matched == 1


def test_vendor_contains_filters_client_side(seeded_store):
    fake = FakeBedrock(
        tool_call(operation="scan_all", aggregation="list", vendor_contains="gym"),
        text_answer("One record from Gym Lounge."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    result = agent.ask("show me the Gym Lounge invoice")
    assert result.matched == 1
    assert result.items[0]["company_name"] == "Gym Lounge"


def test_list_caps_the_sample_sent_to_the_phrasing_call(seeded_store):
    fake = FakeBedrock(
        tool_call(operation="scan_all", aggregation="list"),
        text_answer("Two records."),
    )
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    agent.ask("list everything")
    phrasing_call = fake.calls[1]
    import json
    sent = json.loads(phrasing_call["messages"][0]["content"][0]["text"])
    assert len(sent["data"]["sample_items"]) <= 5


def test_no_tool_call_raises(seeded_store):
    fake = FakeBedrock(text_answer("I'm not sure how to query that."))
    agent = NaturalLanguageQueryAgent(store=seeded_store, client=fake)
    with pytest.raises(BedrockMappingError, match="no run_invoice_query tool call"):
        agent.ask("what's the meaning of life?")


def test_empty_table_returns_zero_not_an_error(store):
    fake = FakeBedrock(
        tool_call(operation="scan_all", aggregation="count"),
        text_answer("No invoices found."),
    )
    agent = NaturalLanguageQueryAgent(store=store, client=fake)
    result = agent.ask("how many invoices?")
    assert result.matched == 0
