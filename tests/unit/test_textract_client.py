"""Pre-flight document checks, these are the ones that run before Textract is ever
called, using nothing but what discovery already knows for free."""

from __future__ import annotations

import pytest

from src.extraction.textract_client import (
    MAX_DOCUMENT_SIZE_BYTES,
    check_document_size,
)
from src.pipeline.errors import TextractExtractionError


def test_a_normal_sized_document_passes():
    check_document_size(68_971, "Order_ID_7104598035.pdf")  # does not raise


def test_zero_byte_document_is_rejected():
    with pytest.raises(TextractExtractionError, match="empty"):
        check_document_size(0, "empty.pdf")


def test_negative_size_is_rejected():
    with pytest.raises(TextractExtractionError, match="empty"):
        check_document_size(-1, "corrupt-metadata.pdf")


def test_document_over_the_async_limit_is_rejected():
    with pytest.raises(TextractExtractionError, match="exceeds Textract's"):
        check_document_size(MAX_DOCUMENT_SIZE_BYTES + 1, "huge.pdf")


def test_document_exactly_at_the_limit_passes():
    check_document_size(MAX_DOCUMENT_SIZE_BYTES, "at-the-limit.pdf")  # does not raise
