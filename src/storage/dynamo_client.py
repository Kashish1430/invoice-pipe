"""DynamoDB load: item shaping, idempotent writes, GSI queries.

Key design (docs/plan.md S4):

    PK = "VENDOR#<normalize(company_name)>"
    SK = "INVOICE#<normalize(invoice_reference_number)>"

so every invoice from one vendor shares a partition, and re-running a batch
rewrites the same keys instead of creating duplicates. The write is conditional
on both keys being absent; a `ConditionalCheckFailedException` is the duplicate
signal, not an error.

Money stays `Decimal` end to end. boto3's serializer raises `TypeError` on
`float`, so `model_dump(mode="python")` is the right dump mode here and
`mode="json"` would break the load -- the Stage-2 Decimal decision and this
layer's requirement are the same decision seen twice.
"""

from __future__ import annotations

from datetime import date as date_, datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src import settings
from src.pipeline.errors import DuplicateRecordError, StorageError
from src.storage.key_normalization import (
    date_gsi1pk,
    date_gsi1sk,
    dedup_key,
    invoice_sk,
    status_gsi2pk,
    vendor_pk,
)

GSI1 = "GSI1-DateIndex"
GSI2 = "GSI2-StatusIndex"


def _dynamo_safe(value: Any) -> Any:
    """Recursively make a value DynamoDB-serialisable.

    Only two things need touching: `float` (rejected outright by boto3 -- the
    Textract confidences arrive as floats) and `date`/`datetime` (no native
    attribute type). Decimals pass through untouched, which is the whole point.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, (date_, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_safe(v) for v in value]
    return value


def build_item(record: Any, *, ingested_at_utc: str | None = None) -> dict[str, Any]:
    """Turn an `InvoiceRecord` into the stored item, keys and all.

    `invoice_reference_number` keeps the vendor's original spacing on the item
    for humans; only the key is normalised. Dates are stored ISO-formatted
    rather than in the vendor's printed format, so range queries and the GSI1
    sort key agree with the attribute.
    """
    ingested = ingested_at_utc or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    pk = vendor_pk(record.company_name)
    sk = invoice_sk(record.invoice_reference_number)
    iso_date = record.date.isoformat()
    status = record.status

    item = _dynamo_safe(record.model_dump(mode="python"))
    item.update(
        {
            "PK": pk,
            "SK": sk,
            "dedup_key": dedup_key(
                record.company_name, record.invoice_reference_number
            ),
            "GSI1PK": date_gsi1pk(iso_date),
            "GSI1SK": date_gsi1sk(iso_date, pk),
            "GSI2PK": status_gsi2pk(status),
            "GSI2SK": ingested,
            "status": status,
            "ingested_at_utc": ingested,
            "schema_version": int(settings.get("storage.schema_version", 1)),
        }
    )
    return item


def quarantine_tombstone(
    *, filename: str, batch_id: str, error_type: str, failed_at_stage: str,
    failed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Minimal item marking a file that never reached storage.

    Quarantined records are filesystem-only by design, which would leave an ops
    dashboard querying two systems. This tombstone keeps pipeline status
    answerable entirely from GSI2.
    """
    at = failed_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "PK": f"QUARANTINE#{batch_id}",
        "SK": f"FILE#{filename}",
        "GSI2PK": status_gsi2pk("QUARANTINED"),
        "GSI2SK": at,
        "status": "QUARANTINED",
        "source_file": filename,
        "batch_id": batch_id,
        "error_type": error_type,
        "failed_at_stage": failed_at_stage,
        "failed_at_utc": at,
        "schema_version": int(settings.get("storage.schema_version", 1)),
    }


def table_definition(table_name: str) -> dict[str, Any]:
    """CreateTable kwargs -- the single source of truth for the key schema.

    Used by the moto-backed integration tests and by infra bootstrap, so the
    indexes the code queries and the indexes that exist cannot drift.
    """
    return {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": GSI1,
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": GSI2,
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    }


class DynamoStore:
    def __init__(self, table=None, *, table_name: str | None = None, resource=None):
        self.table_name = table_name or settings.get("storage.table_name")
        self._table = table
        self._resource = resource

    @property
    def table(self):
        if self._table is None:
            resource = self._resource or boto3.resource(
                "dynamodb", region_name=settings.get("aws.region")
            )
            self._table = resource.Table(self.table_name)
        return self._table

    def create_table_if_missing(self) -> None:
        resource = self._resource or boto3.resource(
            "dynamodb", region_name=settings.get("aws.region")
        )
        try:
            resource.meta.client.describe_table(TableName=self.table_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            resource.create_table(**table_definition(self.table_name))
            # A brand-new table (with its GSIs) is not immediately writable --
            # skipping this wait would race the very first put_item against a
            # table still in CREATING state on a fresh account.
            resource.meta.client.get_waiter("table_exists").wait(
                TableName=self.table_name
            )

    # ---- writes -------------------------------------------------------------
    def put_record(self, record: Any, *, ingested_at_utc: str | None = None) -> dict[str, Any]:
        """Idempotent load. Raises `DuplicateRecordError` if already present."""
        item = build_item(record, ingested_at_utc=ingested_at_utc)
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ConditionalCheckFailedException":
                raise DuplicateRecordError(
                    f"{record.source_file}: {item['dedup_key']} already loaded"
                ) from exc
            raise StorageError(
                f"{record.source_file}: put_item failed ({code}): {exc}"
            ) from exc
        return item

    def put_quarantine_tombstone(self, **kwargs: Any) -> dict[str, Any]:
        item = quarantine_tombstone(**kwargs)
        try:
            self.table.put_item(Item=item)
        except ClientError as exc:
            raise StorageError(f"tombstone write failed: {exc}") from exc
        return item

    # ---- reads --------------------------------------------------------------
    def query_month(self, year_month: str) -> list[dict[str, Any]]:
        """All invoices in a `yyyy-mm` reporting period, via GSI1 -- not a Scan."""
        return self._query(
            IndexName=GSI1,
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"DATE#{year_month}"},
        )

    def query_status(self, status: str) -> list[dict[str, Any]]:
        """Everything in one pipeline outcome bucket, tombstones included."""
        return self._query(
            IndexName=GSI2,
            KeyConditionExpression="GSI2PK = :pk",
            ExpressionAttributeValues={":pk": status_gsi2pk(status)},
        )

    def get_by_key(self, company_name: str, invoice_reference_number: str):
        response = self.table.get_item(
            Key={
                "PK": vendor_pk(company_name),
                "SK": invoice_sk(invoice_reference_number),
            }
        )
        return response.get("Item")

    def scan_all(self) -> list[dict[str, Any]]:
        """Full-table scan for the batch stats job. See `stats/batch_stats.py`
        for why a Scan is the right MVP answer at this table size."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            response = self.table.scan(**kwargs)
            items.extend(response.get("Items") or [])
            key = response.get("LastEvaluatedKey")
            if not key:
                return items
            kwargs["ExclusiveStartKey"] = key

    def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items") or [])
            key = response.get("LastEvaluatedKey")
            if not key:
                return items
            kwargs["ExclusiveStartKey"] = key
