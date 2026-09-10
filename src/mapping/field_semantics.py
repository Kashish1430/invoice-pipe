"""Canonical-field semantics: the Bedrock system prompt and tool input schema.

Both are generated from `config/canonical_schema.yaml`, so the prompt describing
a field and the schema accepting it can never drift apart -- editing the field
list in one YAML file updates both.

Every canonical value is typed `string` in the tool schema, including money and
dates. That is deliberate: the LLM's job is to decide *which* printed value
means `total_amount`, not to reformat it. Coercion to `Decimal`/`date` belongs
to Pydantic, where a failure is a catchable, quarantinable error instead of a
silently plausible number.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from src import settings

TOOL_NAME = "map_invoice_fields"


@lru_cache(maxsize=None)
def load_schema() -> dict[str, Any]:
    with open(settings.canonical_schema_path(), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def reset_cache() -> None:
    load_schema.cache_clear()


def fields() -> list[dict[str, Any]]:
    return list(load_schema().get("fields") or [])


def canonical_field_names() -> tuple[str, ...]:
    return tuple(f["name"] for f in fields())


def required_field_names() -> tuple[str, ...]:
    return tuple(f["name"] for f in fields() if f.get("required"))


def defaults() -> dict[str, str]:
    return {f["name"]: f["default"] for f in fields() if "default" in f}


def tool_input_schema() -> dict[str, Any]:
    """Strict JSON Schema mirroring the 13 canonical fields plus provenance."""
    properties: dict[str, Any] = {}
    for f in fields():
        properties[f["name"]] = {
            "type": "string",
            "description": " ".join(str(f["semantics"]).split()),
        }
    properties.update(
        {
            "source_labels": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Canonical field name -> the verbatim source label its value "
                    "was taken from. Omit fields that were absent from the source."
                ),
            },
            "unmapped_metadata": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Everything extracted with no canonical slot, as flat "
                    "string->string. `address` always belongs here."
                ),
            },
            "multiple_total_candidates": {
                "type": "boolean",
                "description": (
                    "true when two or more source labels plausibly denoted the "
                    "final payable amount and one had to be chosen."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Contradictions, ambiguities, or the rejected alternatives "
                    "behind a disambiguation decision. Empty when there are none."
                ),
            },
        }
    )
    return {
        "type": "object",
        "properties": properties,
        "required": list(canonical_field_names()) + ["source_labels"],
    }


def tool_spec() -> dict[str, Any]:
    """The `toolConfig.tools[0]` entry for the Converse API."""
    return {
        "toolSpec": {
            "name": TOOL_NAME,
            "description": (
                "Map one invoice's extracted label/value pairs onto the fixed "
                "13-field canonical schema."
            ),
            "inputSchema": {"json": tool_input_schema()},
        }
    }


def system_prompt() -> str:
    """Field-by-field semantics plus the standing disambiguation rules."""
    lines = [
        "You map OCR output from arbitrary vendor invoices onto a fixed "
        "13-field canonical schema.",
        "",
        "The input is JSON produced by AWS Textract: label/value pairs in the "
        "vendor's own vocabulary, each with an OCR confidence, plus any "
        "line-item rows. You never see the document itself.",
        "",
        "Rules:",
        "- Call the map_invoice_fields tool exactly once. Never answer in prose.",
        "- Copy values verbatim from the input. Never reformat a date, never "
        "recompute a total, never strip or add digits, never convert a currency.",
        "- Never invent a value that is not in the input. If a field is absent, "
        "use its default below; if it has no default, use an empty string.",
        "- Garbled text from a corrupted font mapping is passed through as-is. "
        "Do not guess what it was meant to say.",
        "- Record the source label you took each value from in source_labels.",
        "- Anything with no canonical slot goes in unmapped_metadata, including "
        "address, tax registration numbers and account numbers.",
        "",
        "Field semantics:",
    ]
    for f in fields():
        default = f' Default if absent: "{f["default"]}".' if "default" in f else ""
        req = " (required)" if f.get("required") else ""
        semantics = " ".join(str(f["semantics"]).split())
        lines.append(f'- {f["name"]}{req}: {semantics}{default}')

    lines += [
        "",
        "Provenance fields:",
    ]
    for p in load_schema().get("provenance_fields") or []:
        lines.append(f'- {p["name"]}: {" ".join(str(p["semantics"]).split())}')
    return "\n".join(lines)
