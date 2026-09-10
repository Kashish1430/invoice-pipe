"""Textract extraction: `AnalyzeExpense` first, FORMS+TABLES as the fallback.

Textract OCRs the *rendered page raster*, never the PDF's embedded text stream.
That is why it runs uniformly on all six samples rather than only on the scanned
ones: it is structurally immune to the corrupted-cmap failure (sample 1) and the
scrambled reading-order failure (sample 2), and there is no text-layer shortcut
worth branching on.

Both calls are async. The 6-page Airtel statement exceeds the synchronous
single-page limit, and running one code path for every document is worth more
than the couple of seconds a sync call would save on the short ones.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src import settings
from src.extraction.raw_shapes import (
    ExtractedDocument,
    from_document_analysis_response,
    from_expense_response,
)
from src.ingestion.s3_uploader import StagedObject
from src.pipeline.errors import TextractExtractionError

_TERMINAL = {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"}

# Async (S3-based) service quotas for PDF/TIFF documents, per AWS Textract's
# published limits (docs.aws.amazon.com/textract/latest/dg/limits-document.html).
# Re-verify against the live Service Quotas console before relying on this for
# a production gate -- these are AWS defaults, not contractual, and are
# per-account values that can be raised or changed.
MAX_DOCUMENT_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_DOCUMENT_PAGES = 3000  # informational only -- see check_document_size()


def check_document_size(size_bytes: int, source_file: str) -> None:
    """Reject a document before spending a Textract call on it, when possible.

    Deliberately limited to what's knowable for free from discovery alone
    (`DiscoveredFile.size_bytes` / `S3DiscoveredFile.size_bytes`) -- page
    count is NOT checked here, because nothing in this codebase parses PDF
    structure, and Textract itself is the only thing that knows a document's
    true page count. A document over `MAX_DOCUMENT_PAGES` still reaches
    Textract and fails server-side as a `ClientError`, caught the same way
    every other extraction failure is (see `_analyze_expense`).
    """
    if size_bytes <= 0:
        raise TextractExtractionError(
            f"{source_file}: file is empty (0 bytes); refusing to call Textract"
        )
    if size_bytes > MAX_DOCUMENT_SIZE_BYTES:
        raise TextractExtractionError(
            f"{source_file}: {size_bytes:,} bytes exceeds Textract's "
            f"{MAX_DOCUMENT_SIZE_BYTES:,}-byte async document limit; "
            "refusing to call Textract"
        )


class TextractClient:
    """Thin wrapper over the two async Textract APIs, plus job polling."""

    def __init__(
        self,
        client=None,
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
        fallback_threshold: float | None = None,
        sleep=time.sleep,
    ):
        self._client = client
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else float(settings.get("textract.poll_interval_seconds", 2.0))
        )
        self.poll_timeout = (
            poll_timeout
            if poll_timeout is not None
            else float(settings.get("textract.poll_timeout_seconds", 300.0))
        )
        self.fallback_threshold = (
            fallback_threshold
            if fallback_threshold is not None
            else float(settings.get("textract.fallback_confidence_threshold", 50.0))
        )
        self._sleep = sleep

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "textract", region_name=settings.get("aws.region")
            )
        return self._client

    # ---- public API ---------------------------------------------------------
    def analyze(self, staged: StagedObject, source_file: str) -> ExtractedDocument:
        """Extract one staged PDF, falling back when the expense parser misses."""
        doc = self._analyze_expense(staged, source_file)
        if doc.is_usable(self.fallback_threshold):
            return doc

        reason = (
            "no summary fields returned"
            if not doc.summary_fields
            else f"mean confidence {doc.mean_confidence():.1f} < {self.fallback_threshold}"
        )
        fallback = self._analyze_document(staged, source_file)
        # Keep the expense pass's normalised enums when it found anything at
        # all: the FORMS parser has no equivalent, so discarding them would cost
        # the mapper its strongest signal.
        fallback.summary_fields = doc.summary_fields + fallback.summary_fields
        fallback.line_items = doc.line_items + fallback.line_items
        fallback.warnings.insert(0, f"AnalyzeExpense insufficient: {reason}")
        if not fallback.summary_fields:
            raise TextractExtractionError(
                f"{source_file}: neither AnalyzeExpense nor AnalyzeDocument "
                f"returned any label/value pairs ({reason})"
            )
        return fallback

    # ---- AnalyzeExpense -----------------------------------------------------
    def _analyze_expense(
        self, staged: StagedObject, source_file: str
    ) -> ExtractedDocument:
        try:
            job_id = self.client.start_expense_analysis(
                DocumentLocation=staged.as_textract_document()
            )["JobId"]
        except ClientError as exc:
            raise TextractExtractionError(
                f"{source_file}: StartExpenseAnalysis failed: {exc}"
            ) from exc

        pages = self._collect(
            self.client.get_expense_analysis, job_id, source_file, "ExpenseDocuments"
        )
        return from_expense_response({"ExpenseDocuments": pages}, source_file, job_id)

    # ---- AnalyzeDocument FORMS+TABLES fallback ------------------------------
    def _analyze_document(
        self, staged: StagedObject, source_file: str
    ) -> ExtractedDocument:
        try:
            job_id = self.client.start_document_analysis(
                DocumentLocation=staged.as_textract_document(),
                FeatureTypes=["FORMS", "TABLES"],
            )["JobId"]
        except ClientError as exc:
            raise TextractExtractionError(
                f"{source_file}: StartDocumentAnalysis failed: {exc}"
            ) from exc

        blocks = self._collect(
            self.client.get_document_analysis, job_id, source_file, "Blocks"
        )
        return from_document_analysis_response(
            {"Blocks": blocks}, source_file, job_id
        )

    # ---- polling ------------------------------------------------------------
    def _collect(
        self, getter, job_id: str, source_file: str, results_key: str
    ) -> list[dict[str, Any]]:
        """Poll to a terminal state, then drain every NextToken page.

        Textract paginates results independently of job status, so a 6-page
        statement can report SUCCEEDED and still hand back its blocks across
        several calls; reading only the first page would silently truncate it.
        """
        deadline = time.monotonic() + self.poll_timeout
        status, response = "IN_PROGRESS", {}
        while status not in _TERMINAL:
            if time.monotonic() > deadline:
                raise TextractExtractionError(
                    f"{source_file}: Textract job {job_id} still {status} after "
                    f"{self.poll_timeout}s"
                )
            try:
                response = getter(JobId=job_id)
            except ClientError as exc:
                raise TextractExtractionError(
                    f"{source_file}: polling {job_id} failed: {exc}"
                ) from exc
            status = response.get("JobStatus", "IN_PROGRESS")
            if status not in _TERMINAL:
                self._sleep(self.poll_interval)

        if status == "FAILED":
            raise TextractExtractionError(
                f"{source_file}: Textract job {job_id} FAILED: "
                f"{response.get('StatusMessage', 'no status message')}"
            )

        results = list(response.get(results_key) or [])
        token = response.get("NextToken")
        while token:
            response = getter(JobId=job_id, NextToken=token)
            results.extend(response.get(results_key) or [])
            token = response.get("NextToken")
        return results
