"""Stages source PDFs to S3.

Textract's async APIs (`StartExpenseAnalysis`, `StartDocumentAnalysis`) only
accept an S3 object, and async is not optional here: the 6-page Airtel statement
exceeds the single-page limit of the synchronous calls. So every document is
staged, not just the multi-page ones -- one code path, no per-document branch.

Keys are `<'incoming' prefix>/<batch_id>/<filename>` so a batch's inputs stay
grouped and a re-run of the same batch overwrites its own objects rather than
accumulating copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import boto3

from src import settings


@dataclass(frozen=True)
class StagedObject:
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    def as_textract_document(self) -> dict[str, dict[str, str]]:
        """The `DocumentLocation` shape Textract's async APIs expect."""
        return {"S3Object": {"Bucket": self.bucket, "Name": self.key}}


class S3Uploader:
    def __init__(self, client=None, bucket: str | None = None, prefix: str | None = None):
        self._client = client
        self.bucket = bucket or settings.get("aws.s3_bucket")
        self.prefix = (prefix or settings.get("aws.s3_incoming_prefix", "incoming")).strip("/")

    @property
    def client(self):
        # Lazily constructed so importing the module never needs credentials --
        # unit tests can construct S3Uploader() without touching AWS.
        if self._client is None:
            self._client = boto3.client("s3", region_name=settings.get("aws.region"))
        return self._client

    def key_for(self, filename: str, batch_id: str) -> str:
        return f"{self.prefix}/{batch_id}/{filename}"

    def stage(self, path: str | Path, batch_id: str) -> StagedObject:
        path = Path(path)
        key = self.key_for(path.name, batch_id)
        self.client.upload_file(
            str(path), self.bucket, key, ExtraArgs={"ContentType": "application/pdf"}
        )
        return StagedObject(bucket=self.bucket, key=key)
