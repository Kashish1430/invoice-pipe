"""Natural-language querying over the invoice_records DynamoDB table.

A user asks a plain-English question ("how many invoices did we quarantine
this month?", "what's the total from Gym Lounge?"); Bedrock -- forced tool
use, the same pattern the extraction-mapping stage already uses -- decides
which of a small, fixed set of *safe, pre-built* DynamoDB read operations
answers it, and with what parameters. The model never writes a query
itself: it picks among `scan_all` / `query_status` / `query_month`, exactly
the same read methods `DynamoStore` already exposes elsewhere in this
project. That's a deliberate constraint, not a limitation worked around --
letting a model generate and execute arbitrary DynamoDB expressions against
a real table is a correctness and safety problem (a malformed or malicious
filter expression, an unbounded Scan) that a fixed menu of operations
avoids entirely.

Any arithmetic (counts, sums) runs in plain Python over `Decimal`, never in
the model -- the same discipline `InvoiceRecord` uses for money, for the
same reason: an LLM is a poor calculator and a good judge of *what* is
being asked.

Two Bedrock calls per question: one to interpret the question into an
operation, one to phrase the computed result back into a sentence. Roughly
double the cost/latency of the single-call mapping stage -- acceptable for
an interactive query tool, not something you'd want inside the daily batch.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import boto3

from src import settings
from src.pipeline.errors import BedrockMappingError
from src.storage.dynamo_client import DynamoStore

TOOL_NAME = "run_invoice_query"

_OPERATIONS = ("scan_all", "query_status", "query_month")
_STATUSES = ("LOADED", "LOADED_WITH_WARNINGS", "QUARANTINED")
_AGGREGATIONS = ("count", "sum_total_amount", "list")

_DISPLAY_FIELDS = (
    "company_name",
    "invoice_reference_number",
    "total_amount",
    "currency",
    "status",
    "date",
)


def _tool_spec() -> dict[str, Any]:
    return {
        "toolSpec": {
            "name": TOOL_NAME,
            "description": (
                "Translate a natural-language question about invoice records "
                "into exactly one safe, pre-built DynamoDB read operation."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": list(_OPERATIONS),
                            "description": (
                                "scan_all: every record, filtered client-side -- "
                                "use for anything not cleanly a status or month "
                                "lookup. query_status: every record with one "
                                "status, via GSI2. query_month: every record "
                                "whose invoice date falls in one yyyy-mm, via GSI1."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": list(_STATUSES),
                            "description": "Required when operation is query_status.",
                        },
                        "year_month": {
                            "type": "string",
                            "description": "yyyy-mm. Required when operation is query_month.",
                        },
                        "vendor_contains": {
                            "type": "string",
                            "description": (
                                "Optional case-insensitive substring filter on "
                                "company_name, applied after the fetch. Empty "
                                "string if the question doesn't name a vendor."
                            ),
                        },
                        "aggregation": {
                            "type": "string",
                            "enum": list(_AGGREGATIONS),
                            "description": (
                                "count: how many records matched. "
                                "sum_total_amount: sum total_amount (GBP) across "
                                "the matched records. list: return each matching "
                                "record's key fields."
                            ),
                        },
                    },
                    "required": ["operation", "aggregation"],
                }
            },
        }
    }


def _system_prompt() -> str:
    return (
        "You translate a plain-English question about invoice records into "
        "exactly one call to " + TOOL_NAME + ". You never invent a query "
        "language and never write a DynamoDB expression yourself -- you only "
        "choose among the pre-built operations the tool schema describes and "
        "supply their parameters. If the question doesn't map cleanly to a "
        "single status or month, choose scan_all with aggregation=list rather "
        "than guess at a narrower filter that might silently exclude records "
        "the user actually wanted to see."
    )


@dataclass
class QueryResult:
    question: str
    operation: str
    aggregation: str
    matched: int
    items: list[dict[str, Any]] = field(default_factory=list)
    total_gbp: Decimal | None = None
    answer: str = ""


class NaturalLanguageQueryAgent:
    """Ask a plain-English question about the invoice table, get one back."""

    def __init__(
        self,
        store: DynamoStore | None = None,
        client: Any = None,
        model_id: str | None = None,
    ):
        self.store = store or DynamoStore()
        self._client = client
        self.model_id = model_id or settings.get("bedrock.model_id")

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime", region_name=settings.get("aws.region")
            )
        return self._client

    def ask(self, question: str) -> QueryResult:
        intent = self._interpret(question)
        items = self._fetch(intent)
        result = self._aggregate(question, intent, items)
        result.answer = self._phrase(result)
        return result

    # ---- step 1: NL -> structured intent, forced tool use --------------
    def _interpret(self, question: str) -> dict[str, Any]:
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": _system_prompt()}],
            messages=[{"role": "user", "content": [{"text": question}]}],
            toolConfig={
                "tools": [_tool_spec()],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )
        content = ((response.get("output") or {}).get("message") or {}).get(
            "content"
        ) or []
        for block in content:
            use = block.get("toolUse")
            if use and use.get("name") == TOOL_NAME:
                return use.get("input") or {}
        raise BedrockMappingError(
            f"no {TOOL_NAME} tool call in response (stopReason={response.get('stopReason')!r})"
        )

    # ---- step 2: run the one operation the model chose -------------------
    def _fetch(self, intent: dict[str, Any]) -> list[dict[str, Any]]:
        op = intent.get("operation")
        if op == "query_status":
            items = self.store.query_status(intent["status"])
        elif op == "query_month":
            items = self.store.query_month(intent["year_month"])
        else:
            items = self.store.scan_all()

        vendor_contains = (intent.get("vendor_contains") or "").strip().lower()
        if vendor_contains:
            items = [
                i
                for i in items
                if vendor_contains in str(i.get("company_name", "")).lower()
            ]
        # Quarantine tombstones (PK="QUARANTINE#...") have no total_amount and
        # aren't invoices -- only count/sum/list them when the question was
        # explicitly about quarantined status, matching what a human would
        # expect "how many invoices" to mean otherwise.
        if op != "query_status" or intent.get("status") != "QUARANTINED":
            items = [i for i in items if not str(i.get("PK", "")).startswith("QUARANTINE#")]
        return items

    # ---- step 3: deterministic aggregation -- never the model's math -----
    def _aggregate(
        self, question: str, intent: dict[str, Any], items: list[dict[str, Any]]
    ) -> QueryResult:
        agg = intent.get("aggregation", "list")
        total = None
        if agg == "sum_total_amount":
            total = sum(
                (i.get("total_amount") for i in items if isinstance(i.get("total_amount"), Decimal)),
                Decimal("0"),
            )
        return QueryResult(
            question=question,
            operation=intent.get("operation", "scan_all"),
            aggregation=agg,
            matched=len(items),
            items=items,
            total_gbp=total,
        )

    # ---- step 4: phrase the computed result back in a sentence -----------
    def _phrase(self, result: QueryResult) -> str:
        data = {
            "matched": result.matched,
            "total_gbp": str(result.total_gbp) if result.total_gbp is not None else None,
            "sample_items": [
                {k: str(v) for k, v in i.items() if k in _DISPLAY_FIELDS}
                for i in result.items[:5]
            ],
            "truncated": result.matched > 5 and result.aggregation == "list",
        }
        response = self.client.converse(
            modelId=self.model_id,
            system=[{
                "text": (
                    "Answer the user's question in one or two plain sentences, "
                    "using only the JSON data given. Never state a number that "
                    "isn't in the data. If sample_items is truncated, say so."
                )
            }],
            messages=[{
                "role": "user",
                "content": [{"text": json.dumps({"question": result.question, "data": data})}],
            }],
            inferenceConfig={"maxTokens": 256, "temperature": 0.0},
        )
        content = ((response.get("output") or {}).get("message") or {}).get(
            "content"
        ) or []
        for block in content:
            if "text" in block:
                return block["text"].strip()
        return f"Found {result.matched} matching record(s)."  # defensive fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask a natural-language question about the invoice table."
    )
    parser.add_argument("question", help="e.g. 'What is the total from Coffee Culture?'")
    args = parser.parse_args(argv)

    result = NaturalLanguageQueryAgent().ask(args.question)
    print(result.answer)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
