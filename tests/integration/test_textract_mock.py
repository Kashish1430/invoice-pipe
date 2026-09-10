"""Textract leg against stubbed responses shaped like the six real samples.

The stub implements the async contract the client actually depends on a job
id, an IN_PROGRESS phase, then paginated results so polling, pagination and
the FORMS+TABLES fallback are exercised rather than mocked away.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from src.extraction.raw_shapes import (
    from_document_analysis_response,
    from_expense_response,
)
from src.extraction.textract_client import TextractClient
from src.ingestion.s3_uploader import StagedObject
from src.pipeline.errors import TextractExtractionError

STAGED = StagedObject(bucket="test-bucket", key="incoming/b1/doc.pdf")


class FakeTextract:
    """Minimal stand-in for the async Textract API."""

    def __init__(
        self,
        expense_pages=None,
        document_pages=None,
        in_progress_polls=1,
        expense_status="SUCCEEDED",
        start_error=None,
    ):
        self.expense_pages = expense_pages or []
        self.document_pages = document_pages or []
        self.in_progress_polls = in_progress_polls
        self.expense_status = expense_status
        self.start_error = start_error
        self.calls: list[str] = []
        self._polls = 0

# -- start 
    def start_expense_analysis(self, **kwargs):
        self.calls.append("start_expense_analysis")
        if self.start_error:
            raise self.start_error
        return {"JobId": "job-expense"}

    def start_document_analysis(self, **kwargs):
        self.calls.append("start_document_analysis")
        assert set(kwargs["FeatureTypes"]) == {"FORMS", "TABLES"}
        return {"JobId": "job-document"}

# -- poll
    def _paged(self, pages, token, key, status):
        if not pages:
            return {"JobStatus": status, key: []}
        index = 0 if token is None else int(token)
        response = {"JobStatus": status, key: pages[index]}
        if index + 1 < len(pages):
            response["NextToken"] = str(index + 1)
        return response

    def get_expense_analysis(self, JobId, NextToken=None):
        self.calls.append("get_expense_analysis")
        if NextToken is None and self._polls < self.in_progress_polls:
            self._polls += 1
            return {"JobStatus": "IN_PROGRESS"}
        return self._paged(
            self.expense_pages, NextToken, "ExpenseDocuments", self.expense_status
        )

    def get_document_analysis(self, JobId, NextToken=None):
        self.calls.append("get_document_analysis")
        return self._paged(self.document_pages, NextToken, "Blocks", "SUCCEEDED")


def client(fake):
    return TextractClient(client=fake, poll_interval=0, poll_timeout=5, sleep=lambda _: None)


# parsers, against the committed fixtures

def test_vfs_sample_parses_its_documented_quirks(textract_response):
    doc = from_expense_response(
        textract_response("2324GBRAMD125920"), "2324GBRAMD125920.pdf"
    )
    values = {f.label: f.value for f in doc.summary_fields}
    assert values["Grand Total:"] == "0.00"
    assert values["Amount:"] == "1 1 1,333.00"  # kerning split survives extraction
    assert doc.confidence_by_label()["Applicant Name:"] == 41.2
    assert len(doc.line_items) == 3


def test_airtel_sample_keeps_every_competing_total(textract_response):
    doc = from_expense_response(
        textract_response("7042968270_543523577_4_2026"),
        "7042968270_543523577_4_2026.pdf",
    )
    labels = {f.label for f in doc.summary_fields}
    assert {
        "Total Amount Payable:",
        "Last bill amount",
        "This month's charges",
        "Amount after due date (22 Apr)",
        "Total Amount",
        "Grand Total",
    } <= labels
    # Extraction must not pick a winner; disambiguation is Stage 2's job.
    assert doc.page_count == 2  # two expense documents in the response


def test_gym_lounge_sample_carries_both_printings_of_its_invoice_number(
    textract_response,
):
    doc = from_expense_response(
        textract_response("Invoice1653194348"), "Invoice1653194348.pdf"
    )
    values = {f.label: f.value for f in doc.summary_fields}
    assert values["Tax Invoice"] == "Gym Lounge//2022-2023/240"
    assert values["Invoice/Receipt No."] == "Gym Lounge/ / 2022- 2023/ 240"
    assert values["GST NO :"] == "null"


def test_forms_and_tables_fallback_parser_rebuilds_kv_pairs_and_rows(
    textract_response,
):
    """`_forms_fallback_sample` is synthetic (a scan structured to force the
    FORMS+TABLES fallback), not tied to any real invoice(s) that are  kept separate
    from `Order_ID_7104598035`, which now holds real captured AWS output
    that never exercises this path (its real AnalyzeExpense call succeeded
    outright, no fallback needed)."""
    doc = from_document_analysis_response(
        textract_response("_forms_fallback_sample"), "synthetic-fallback-sample.pdf"
    )
    values = {f.label: f.value for f in doc.summary_fields}
    assert values["Order ID"] == "7104598035"
    assert values["Grand Total"] == "4120.00"
    assert [li.fields["Description"] for li in doc.line_items] == [
        "Silk Saree - Kanjivaram",
        "Cotton Dupatta",
    ]
    assert doc.warnings  # the fallback flags its own unnormalised labels


def test_round_trip_through_the_silver_shape(textract_response):
    doc = from_expense_response(
        textract_response("Invoice1653194348"), "Invoice1653194348.pdf"
    )
    from src.extraction.raw_shapes import ExtractedDocument

    restored = ExtractedDocument.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()


# the async client
def test_client_polls_until_terminal_then_returns(textract_response, pipeline_env):
    response = textract_response("Invoice1653194348")
    fake = FakeTextract(expense_pages=[response["ExpenseDocuments"]], in_progress_polls=2)
    doc = client(fake).analyze(STAGED, "Invoice1653194348.pdf")
    assert doc.api == "AnalyzeExpense"
    assert fake.calls.count("get_expense_analysis") == 3  # two IN_PROGRESS, one result


def test_client_drains_every_result_page(textract_response, pipeline_env):
    """A SUCCEEDED job can still paginate; reading page one would truncate it."""
    docs = textract_response("7042968270_543523577_4_2026")["ExpenseDocuments"]
    fake = FakeTextract(expense_pages=[[docs[0]], [docs[1]]], in_progress_polls=0)
    doc = client(fake).analyze(STAGED, "airtel.pdf")
    labels = {f.label for f in doc.summary_fields}
    assert "Total Amount Payable:" in labels  # page 1
    assert "Grand Total" in labels             # page 2


def test_empty_expense_result_falls_back_to_forms_and_tables(
    textract_response, pipeline_env
):
    blocks = textract_response("_forms_fallback_sample")["Blocks"]
    fake = FakeTextract(expense_pages=[], document_pages=[blocks], in_progress_polls=0)
    doc = client(fake).analyze(STAGED, "synthetic-fallback-sample.pdf")
    assert "start_document_analysis" in fake.calls
    assert doc.api == "AnalyzeDocument"
    assert any("AnalyzeExpense insufficient" in w for w in doc.warnings)


def test_low_confidence_expense_result_also_falls_back(pipeline_env):
    weak = [
        {
            "ExpenseIndex": 1,
            "SummaryFields": [
                {
                    "Type": {"Text": "OTHER"},
                    "LabelDetection": {"Text": "Total", "Confidence": 20.0},
                    "ValueDetection": {"Text": "12.00", "Confidence": 20.0},
                }
            ],
        }
    ]
    blocks = [
        {"Id": "k", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"],
         "Confidence": 95.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["kw"]}, {"Type": "VALUE", "Ids": ["v"]}]},
        {"Id": "kw", "BlockType": "WORD", "Text": "Total"},
        {"Id": "v", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"],
         "Confidence": 95.0, "Relationships": [{"Type": "CHILD", "Ids": ["vw"]}]},
        {"Id": "vw", "BlockType": "WORD", "Text": "12.00"},
    ]
    fake = FakeTextract(expense_pages=[weak], document_pages=[blocks], in_progress_polls=0)
    doc = client(fake).analyze(STAGED, "weak.pdf")
    assert "start_document_analysis" in fake.calls
    # The weak expense fields are kept: their normalised enums are still the
    # only typed signal the mapper gets.
    assert len(doc.summary_fields) == 2


def test_both_apis_empty_is_a_hard_failure(pipeline_env):
    fake = FakeTextract(expense_pages=[], document_pages=[], in_progress_polls=0)
    with pytest.raises(TextractExtractionError, match="returned any label/value"):
        client(fake).analyze(STAGED, "blank.pdf")


def test_failed_job_raises(pipeline_env):
    fake = FakeTextract(expense_pages=[[]], expense_status="FAILED", in_progress_polls=0)
    with pytest.raises(TextractExtractionError, match="FAILED"):
        client(fake).analyze(STAGED, "broken.pdf")


def test_start_error_raises_extraction_error(pipeline_env):
    error = ClientError(
        {"Error": {"Code": "InvalidS3ObjectException", "Message": "nope"}},
        "StartExpenseAnalysis",
    )
    fake = FakeTextract(start_error=error)
    with pytest.raises(TextractExtractionError, match="StartExpenseAnalysis failed"):
        client(fake).analyze(STAGED, "missing.pdf")


def test_poll_timeout_raises(pipeline_env):
    fake = FakeTextract(expense_pages=[[]], in_progress_polls=10_000)
    slow = TextractClient(
        client=fake, poll_interval=0, poll_timeout=-1, sleep=lambda _: None
    )
    with pytest.raises(TextractExtractionError, match="still IN_PROGRESS"):
        slow.analyze(STAGED, "slow.pdf")


