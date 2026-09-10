"""Semantic mapping: Bedrock Converse with a forced tool call.

Textract returns the vendor's own vocabulary and cannot decide what it *means* --
which of four "total"-labelled figures on the Airtel statement is the amount
actually payable, or that "Member Name" is the buyer. That judgement is the only
thing this stage exists to make.

The call is text-only. Textract has already done the OCR, and better; paying
image-token cost to have a vision model redo it would be slower, dearer and less
reliable on dense numeric tables. The input here is small JSON, which is what
keeps the Bedrock leg at roughly a fifth of the Textract leg's cost.

Tool use is *forced* (`toolChoice: {"tool": ...}`) rather than requested. A
forced call means the response is schema-shaped by construction, so there is no
prose-to-JSON parsing step to fail on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src import settings
from src.extraction.raw_shapes import ExtractedDocument
from src.mapping import field_semantics
from src.mapping.vendor_alias_cache import VendorAliasCache
from src.pipeline.errors import BedrockMappingError


@dataclass
class MappedPayload:
    """Bedrock's tool input, plus how it was obtained."""

    fields: dict[str, str] = field(default_factory=dict)
    source_labels: dict[str, str] = field(default_factory=dict)
    unmapped_metadata: dict[str, str] = field(default_factory=dict)
    multiple_total_candidates: bool = False
    notes: str = ""
    model_id: str = ""
    source: str = "bedrock"  # or "vendor_alias_cache"
    attempts: int = 1
    # Real token counts from the Converse API's own `usage` block -- 0 when
    # `source` isn't "bedrock" (a cache hit made no call at all, so there is
    # nothing to report; never estimated or backfilled).
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": self.fields,
            "source_labels": self.source_labels,
            "unmapped_metadata": self.unmapped_metadata,
            "multiple_total_candidates": self.multiple_total_candidates,
            "notes": self.notes,
            "model_id": self.model_id,
            "source": self.source,
            "attempts": self.attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MappedPayload":
        return cls(
            fields=dict(payload.get("fields") or {}),
            source_labels=dict(payload.get("source_labels") or {}),
            unmapped_metadata=dict(payload.get("unmapped_metadata") or {}),
            multiple_total_candidates=bool(payload.get("multiple_total_candidates")),
            notes=payload.get("notes", ""),
            model_id=payload.get("model_id", ""),
            source=payload.get("source", "unknown"),
            attempts=int(payload.get("attempts", 1)),
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
        )


def _document_message(doc: ExtractedDocument) -> str:
    """The compact JSON view of one document handed to the model."""
    return json.dumps(
        {
            "source_file": doc.source_file,
            "extracted_by": doc.api,
            "page_count": doc.page_count,
            "warnings": doc.warnings,
            "label_value_pairs": doc.label_value_pairs(),
            "line_items": [li.fields for li in doc.line_items],
        },
        ensure_ascii=False,
        indent=None,
    )


class BedrockMapper:
    """Maps one `ExtractedDocument` onto the canonical 13-field shape."""

    def __init__(
        self,
        client=None,
        *,
        model_id: str | None = None,
        escalation_model_id: str | None = None,
        max_retries: int | None = None,
        cache: VendorAliasCache | None = None,
    ):
        self._client = client
        self.model_id = model_id or settings.get("bedrock.model_id")
        self.escalation_model_id = escalation_model_id or settings.get(
            "bedrock.escalation_model_id", self.model_id
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(settings.get("bedrock.max_retries", 1))
        )
        self.cache = cache

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime", region_name=settings.get("aws.region")
            )
        return self._client

    # ---- public API ---------------------------------------------------------
    def map_document(self, doc: ExtractedDocument) -> MappedPayload:
        cached = self.cache.apply(doc) if self.cache else None
        if cached is not None:
            return self._finalize(MappedPayload.from_dict(cached), doc)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            # Escalate to the stronger model only once the cheap one has
            # actually failed -- the retry is where the cost is justified.
            model_id = self.model_id if attempt == 0 else self.escalation_model_id
            try:
                payload = self._invoke(doc, model_id)
                payload.attempts = attempt + 1
                if self.cache:
                    self.cache.store(payload)
                return self._finalize(payload, doc)
            except (BedrockMappingError, ClientError) as exc:
                last_error = exc

        raise BedrockMappingError(
            f"{doc.source_file}: mapping failed after {self.max_retries + 1} "
            f"attempt(s): {last_error}"
        ) from last_error

    # ---- internals ----------------------------------------------------------
    def _invoke(self, doc: ExtractedDocument, model_id: str) -> MappedPayload:
        response = self.client.converse(
            modelId=model_id,
            system=[{"text": field_semantics.system_prompt()}],
            messages=[{"role": "user", "content": [{"text": _document_message(doc)}]}],
            toolConfig={
                "tools": [field_semantics.tool_spec()],
                "toolChoice": {"tool": {"name": field_semantics.TOOL_NAME}},
            },
            inferenceConfig={
                "maxTokens": int(settings.get("bedrock.max_tokens", 4096)),
                "temperature": float(settings.get("bedrock.temperature", 0.0)),
            },
        )
        tool_input = self._extract_tool_input(response, doc.source_file)
        payload = self._to_payload(tool_input, model_id, doc.source_file)
        usage = response.get("usage") or {}
        payload.input_tokens = int(usage.get("inputTokens", 0))
        payload.output_tokens = int(usage.get("outputTokens", 0))
        return payload

    @staticmethod
    def _extract_tool_input(response: dict[str, Any], source_file: str) -> dict[str, Any]:
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        for block in content:
            use = block.get("toolUse")
            if use and use.get("name") == field_semantics.TOOL_NAME:
                return use.get("input") or {}
        stop = response.get("stopReason")
        raise BedrockMappingError(
            f"{source_file}: no {field_semantics.TOOL_NAME} tool call in response "
            f"(stopReason={stop!r})"
        )

    @staticmethod
    def _to_payload(
        tool_input: dict[str, Any], model_id: str, source_file: str
    ) -> MappedPayload:
        names = field_semantics.canonical_field_names()
        defaults = field_semantics.defaults()

        missing = [
            n
            for n in field_semantics.required_field_names()
            if not str(tool_input.get(n, "")).strip()
        ]
        if missing:
            # Not a validation warning: a required field the model left blank
            # means the tool schema was not honoured, so the retry is worth it.
            raise BedrockMappingError(
                f"{source_file}: tool call omitted required field(s): "
                f"{', '.join(missing)}"
            )

        fields_out: dict[str, str] = {}
        for n in names:
            raw = tool_input.get(n)
            value = "" if raw is None else str(raw).strip()
            fields_out[n] = value if value else defaults.get(n, "")

        return MappedPayload(
            fields=fields_out,
            source_labels={
                k: str(v)
                for k, v in (tool_input.get("source_labels") or {}).items()
                if k in names
            },
            unmapped_metadata={
                str(k): str(v)
                for k, v in (tool_input.get("unmapped_metadata") or {}).items()
            },
            multiple_total_candidates=bool(tool_input.get("multiple_total_candidates")),
            notes=str(tool_input.get("notes") or ""),
            model_id=model_id,
            source="bedrock",
        )

    @staticmethod
    def _finalize(payload: MappedPayload, doc: ExtractedDocument) -> MappedPayload:
        """Attach what the deterministic layer knows better than the model.

        `product_name` is a flat string by spec, but the Gym Lounge and Airtel
        documents have real multi-row tables. The rows are preserved losslessly
        here, straight from Textract's geometry rather than round-tripped
        through the model, so nothing is lost to the flattening.
        """
        if doc.line_items and "line_items" not in payload.unmapped_metadata:
            payload.unmapped_metadata["line_items"] = json.dumps(
                [li.fields for li in doc.line_items], ensure_ascii=False
            )
        payload.unmapped_metadata.setdefault("extracted_by", doc.api)
        return payload
