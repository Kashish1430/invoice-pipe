"""Mapping leg: forced tool use, retry/escalation, and the vendor alias cache.

Not named in the Stage-5 test layout, but the mapping call is where the
pipeline's only genuine judgement happens, it answers questions such as  which of 
Airtel's six totals is canonical, so the contract around it (tool forced, schema 
honoured, retry on a malformed call) is worth pinning down.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from src.extraction.raw_shapes import from_expense_response
from src.mapping import field_semantics
from src.mapping.bedrock_client import BedrockMapper
from src.mapping.vendor_alias_cache import VendorAliasCache
from src.pipeline.errors import BedrockMappingError


class FakeBedrock:
    """Returns queued tool-use responses and records how it was called."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def tool_response(tool_input, name=field_semantics.TOOL_NAME):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"name": name, "input": tool_input}}],
            }
        },
        "stopReason": "tool_use",
    }


def prose_response():
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "sure!"}]}},
        "stopReason": "end_turn",
    }


@pytest.fixture
def gym_doc(textract_response):
    return from_expense_response(
        textract_response("Invoice1653194348"), "Invoice1653194348.pdf"
    )


@pytest.fixture
def gym_tool_input(mapped_fixture):
    payload = mapped_fixture("Invoice1653194348")
    return {
        **payload["fields"],
        "source_labels": payload["source_labels"],
        "unmapped_metadata": payload["unmapped_metadata"],
        "multiple_total_candidates": payload["multiple_total_candidates"],
        "notes": payload["notes"],
    }



# the schema and prompt handed to the model
def test_schema_declares_all_thirteen_canonical_fields(pipeline_env):
    schema = field_semantics.tool_input_schema()
    assert len(field_semantics.canonical_field_names()) == 13
    for name in field_semantics.canonical_field_names():
        assert schema["properties"][name]["type"] == "string"


def test_money_and_dates_are_strings_in_the_schema(pipeline_env):
    """Coercion belongs to Pydantic, where a bad value is catchable."""
    props = field_semantics.tool_input_schema()["properties"]
    for name in ("date", "total_amount", "vat_tax_amount"):
        assert props[name]["type"] == "string"


def test_address_has_no_top_level_slot(pipeline_env):
    assert "address" not in field_semantics.tool_input_schema()["properties"]


def test_prompt_and_schema_come_from_the_same_yaml(pipeline_env):
    prompt = field_semantics.system_prompt()
    for name in field_semantics.canonical_field_names():
        assert name in prompt

# the call
def test_tool_use_is_forced(pipeline_env, gym_doc, gym_tool_input):
    fake = FakeBedrock(tool_response(gym_tool_input))
    BedrockMapper(client=fake).map_document(gym_doc)
    config = fake.calls[0]["toolConfig"]
    assert config["toolChoice"] == {"tool": {"name": field_semantics.TOOL_NAME}}
    assert config["tools"][0]["toolSpec"]["name"] == field_semantics.TOOL_NAME


def test_the_model_receives_text_not_an_image(pipeline_env, gym_doc, gym_tool_input):
    fake = FakeBedrock(tool_response(gym_tool_input))
    BedrockMapper(client=fake).map_document(gym_doc)
    content = fake.calls[0]["messages"][0]["content"]
    assert list(content[0]) == ["text"]
    payload = json.loads(content[0]["text"])
    assert payload["source_file"] == "Invoice1653194348.pdf"
    assert any(p["label"] == "Payable Amount:" for p in payload["label_value_pairs"])


def test_mapped_payload_shape(pipeline_env, gym_doc, gym_tool_input):
    fake = FakeBedrock(tool_response(gym_tool_input))
    payload = BedrockMapper(client=fake).map_document(gym_doc)
    assert payload.fields["total_amount"] == "6000.00"
    assert payload.source_labels["total_amount"] == "Payable Amount:"
    assert payload.unmapped_metadata["address"].startswith("4th Floor")
    assert payload.multiple_total_candidates is True
    assert payload.attempts == 1


def test_line_items_are_preserved_losslessly(pipeline_env, gym_doc, gym_tool_input):
    """product_name is flat by spec, the real rows must not be lost to that."""
    fake = FakeBedrock(tool_response(gym_tool_input))
    payload = BedrockMapper(client=fake).map_document(gym_doc)
    rows = json.loads(payload.unmapped_metadata["line_items"])
    assert rows[0]["Plan Name"] == "Gym Workout"
    assert rows[0]["Amount"] == "15000.00"


def test_missing_optional_fields_fall_back_to_their_defaults(
    pipeline_env, gym_doc, gym_tool_input
):
    sparse = dict(gym_tool_input)
    for key in ("product_name", "mode_of_payment", "vat_tax_label"):
        sparse.pop(key)
    fake = FakeBedrock(tool_response(sparse))
    payload = BedrockMapper(client=fake).map_document(gym_doc)
    assert payload.fields["product_name"] == "N/A"
    assert payload.fields["mode_of_payment"] == "N/A"


# failure and escalation

def test_missing_required_field_triggers_a_retry_on_the_stronger_model(
    pipeline_env, gym_doc, gym_tool_input
):
    broken = {**gym_tool_input, "company_name": ""}
    fake = FakeBedrock(tool_response(broken), tool_response(gym_tool_input))
    payload = BedrockMapper(client=fake).map_document(gym_doc)
    assert [c["modelId"] for c in fake.calls] == ["test-haiku", "test-sonnet"]
    assert payload.attempts == 2


def test_prose_answer_is_a_mapping_failure(pipeline_env, gym_doc):
    fake = FakeBedrock(prose_response(), prose_response())
    with pytest.raises(BedrockMappingError, match="no map_invoice_fields tool call"):
        BedrockMapper(client=fake).map_document(gym_doc)


def test_client_error_is_retried_then_surfaced(pipeline_env, gym_doc):
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )
    fake = FakeBedrock(error, error)
    with pytest.raises(BedrockMappingError, match="after 2 attempt"):
        BedrockMapper(client=fake).map_document(gym_doc)
    assert len(fake.calls) == 2


# vendor alias cache -- accelerator only
def test_cache_is_populated_then_skips_the_model(
    pipeline_env, gym_doc, gym_tool_input, textract_response
):
    cache = VendorAliasCache(path=pipeline_env / "cache.json")
    fake = FakeBedrock(tool_response(gym_tool_input))
    mapper = BedrockMapper(client=fake, cache=cache)

    first = mapper.map_document(gym_doc)
    assert first.source == "bedrock"

    second_doc = from_expense_response(
        textract_response("Invoice1653194348"), "Invoice1653194348.pdf"
    )
    second = mapper.map_document(second_doc)
    assert second.source == "vendor_alias_cache"
    assert len(fake.calls) == 1  # no second model call
    assert second.fields["total_amount"] == "6000.00"


def test_unknown_vendor_always_goes_to_the_model(pipeline_env, textract_response):
    cache = VendorAliasCache(path=pipeline_env / "cache.json")
    doc = from_expense_response(
        textract_response("2324GBRAMD125920"), "2324GBRAMD125920.pdf"
    )
    assert cache.apply(doc) is None


def test_cache_defers_when_it_cannot_fill_a_required_field(
    pipeline_env, gym_doc, gym_tool_input
):
    """A partial mapping costs one model call; guessing costs a wrong record."""
    thin = {
        **gym_tool_input,
        "source_labels": {"company_name": "Company", "total_amount": "Payable Amount:"},
    }
    cache = VendorAliasCache(path=pipeline_env / "cache.json", min_coverage=0.0)
    fake = FakeBedrock(tool_response(thin), tool_response(gym_tool_input))
    mapper = BedrockMapper(client=fake, cache=cache)
    mapper.map_document(gym_doc)
    assert mapper.map_document(gym_doc).source == "bedrock"
    assert len(fake.calls) == 2


def test_corrupt_cache_file_is_treated_as_empty(pipeline_env):
    path = pipeline_env / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    assert VendorAliasCache(path=path)._entries == {}


