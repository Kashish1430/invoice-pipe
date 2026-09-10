"""Confidence lookup: normalized labels, and coverage for line-item fields.

Pins down the real gap found processing `Order_ID_7104598035.pdf` 
against live Textract/Bedrock (docs/challenges.md): a source label Bedrock
cleaned up ("Coupon (TASTENEW)") failed to match Textract's raw, undecorated
one ("-\\nCoupon (TASTENEW)"), and a value sourced from a line-item column
(product_amount, from "Unit Price") had no confidence lookup path at all,
`confidence_by_label()` only ever walked summary fields.
"""

from __future__ import annotations

from src.extraction.raw_shapes import ExtractedDocument, LineItem, SummaryField, normalize_label
from src.mapping.bedrock_client import MappedPayload
from src.pipeline.run_batch import resolve_confidences


# ---------------------------------------------------------------------------
# normalize_label
def test_strips_leading_dash_and_newline():
    """The exact real string from the Zomato receipt's DISCOUNT field."""
    assert normalize_label("-\nCoupon (TASTENEW)") == "Coupon (TASTENEW)"


def test_collapses_internal_whitespace():
    assert normalize_label("Invoice   Date:\n") == "Invoice Date:"


def test_strips_leading_bullet():
    assert normalize_label("•  Vendor Name") == "Vendor Name"


def test_already_clean_label_is_unchanged():
    assert normalize_label("SGST(9%):") == "SGST(9%):"


def test_empty_and_whitespace_only_labels_normalize_to_empty():
    assert normalize_label("") == ""
    assert normalize_label("   \n  ") == ""


# ---------------------------------------------------------------------------
# ExtractedDocument.confidence_by_label -- now normalized
def test_confidence_by_label_matches_a_decorated_source_label():
    doc = ExtractedDocument(
        source_file="x.pdf",
        summary_fields=[
            SummaryField(
                type_="DISCOUNT", label="-\nCoupon (TASTENEW)", value="(₹52.50)", confidence=99.85
            )
        ],
    )
    by_label = doc.confidence_by_label()
    assert by_label.get("Coupon (TASTENEW)") == 99.85


def test_confidence_by_label_keeps_the_max_across_duplicate_normalized_labels():
    doc = ExtractedDocument(
        source_file="x.pdf",
        summary_fields=[
            SummaryField(type_="TOTAL", label="Total", value="1", confidence=80.0),
            SummaryField(type_="TOTAL", label="-Total", value="1", confidence=95.0),
        ],
    )
    assert doc.confidence_by_label()["Total"] == 95.0


# ExtractedDocument.line_item_confidence_by_label -- the new lookup
def test_line_item_confidence_by_label_covers_a_single_row():
    doc = ExtractedDocument(
        source_file="x.pdf",
        line_items=[
            LineItem(
                fields={"Item": "Masala Soda", "Unit Price": "₹175"},
                field_confidences={"Item": 99.1, "Unit Price": 98.4},
                confidence=98.75,
                row_index=0,
            )
        ],
    )
    by_label = doc.line_item_confidence_by_label()
    assert by_label["Item"] == 99.1
    assert by_label["Unit Price"] == 98.4


def test_line_item_confidence_by_label_takes_the_minimum_across_rows():
    """product_name can join several rows into one string (docs/tradeoffs.md
    #9) and the honest confidence for that join is the weakest row, not an
    average that would hide it behind stronger ones."""
    doc = ExtractedDocument(
        source_file="x.pdf",
        line_items=[
            LineItem(fields={"Item": "A"}, field_confidences={"Item": 99.0}, row_index=0),
            LineItem(fields={"Item": "B"}, field_confidences={"Item": 61.0}, row_index=1),
            LineItem(fields={"Item": "C"}, field_confidences={"Item": 95.0}, row_index=2),
        ],
    )
    assert doc.line_item_confidence_by_label()["Item"] == 61.0


def test_line_item_confidence_by_label_is_empty_for_a_document_with_no_line_items():
    doc = ExtractedDocument(source_file="x.pdf")
    assert doc.line_item_confidence_by_label() == {}



# resolve_confidences -- the end-to-end reproduction of the real bug
def test_resolve_confidences_matches_a_decorated_label():
    doc = ExtractedDocument(
        source_file="x.pdf",
        summary_fields=[
            SummaryField(
                type_="DISCOUNT", label="-\nCoupon (TASTENEW)", value="(₹52.50)", confidence=99.85
            )
        ],
    )
    payload = MappedPayload(
        fields={"coupon_discount_amount": "52.50"},
        source_labels={"coupon_discount_amount": "Coupon (TASTENEW)"},
    )
    assert resolve_confidences(payload, doc) == {"coupon_discount_amount": 99.85}


def test_resolve_confidences_falls_back_to_line_item_columns():
    """The exact real gap: product_amount sourced from a line item's 'Unit
    Price' column had no confidence at all before this fix."""
    doc = ExtractedDocument(
        source_file="x.pdf",
        line_items=[
            LineItem(
                fields={"Item": "Masala Soda", "Unit Price": "₹175"},
                field_confidences={"Item": 99.1, "Unit Price": 98.4},
                row_index=0,
            )
        ],
    )
    payload = MappedPayload(
        fields={"product_name": "Masala Soda", "product_amount": "175"},
        source_labels={"product_name": "Item", "product_amount": "Unit Price"},
    )
    resolved = resolve_confidences(payload, doc)
    assert resolved == {"product_name": 99.1, "product_amount": 98.4}


def test_resolve_confidences_prefers_summary_field_over_line_item_on_collision():
    """If a label somehow exists in both (unusual, but not impossible),
    the header/footer reading but not a table row, is the more reliable one."""
    doc = ExtractedDocument(
        source_file="x.pdf",
        summary_fields=[SummaryField(type_="OTHER", label="Amount", value="1", confidence=90.0)],
        line_items=[
            LineItem(fields={"Amount": "1"}, field_confidences={"Amount": 40.0}, row_index=0)
        ],
    )
    payload = MappedPayload(
        fields={"total_amount": "1"}, source_labels={"total_amount": "Amount"}
    )
    assert resolve_confidences(payload, doc) == {"total_amount": 90.0}


def test_resolve_confidences_omits_a_field_with_no_match_anywhere():
    doc = ExtractedDocument(source_file="x.pdf")
    payload = MappedPayload(
        fields={"mode_of_payment": "N/A"}, source_labels={"mode_of_payment": "Payment Type"}
    )
    assert resolve_confidences(payload, doc) == {}
