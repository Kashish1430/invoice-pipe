"""File discovery and S3 staging."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import settings
from src.ingestion.file_discovery import describe, discover_files, new_batch_id
from src.ingestion.s3_uploader import S3Uploader, StagedObject


@pytest.fixture
def raw_dir(pipeline_env):
    root = settings.data_path("raw")
    (root / "b.pdf").write_bytes(b"%PDF-1.4 second\n")
    (root / "a.pdf").write_bytes(b"%PDF-1.4 first\n")
    (root / "notes.txt").write_bytes(b"not a pdf at all")
    (root / "fake.pdf").write_bytes(b"this is not really a pdf")
    return root


def test_discovery_is_sorted_so_batches_are_reproducible(raw_dir):
    assert [f.filename for f in discover_files(raw_dir)] == [
        "a.pdf",
        "b.pdf",
        "fake.pdf",
    ]


def test_non_pdf_extensions_are_ignored(raw_dir):
    assert "notes.txt" not in [f.filename for f in discover_files(raw_dir)]


def test_a_pdf_without_the_magic_header_is_yielded_and_marked(raw_dir):
    """Yielded rather than skipped, so the orchestrator can quarantine it with
    a reason instead of the file vanishing from the batch silently."""
    files = {f.filename: f for f in discover_files(raw_dir)}
    assert files["fake.pdf"].has_pdf_magic is False
    assert files["a.pdf"].has_pdf_magic is True


def test_nested_directories_are_walked(raw_dir):
    nested = raw_dir / "2026-09" / "batch1"
    nested.mkdir(parents=True)
    (nested / "c.pdf").write_bytes(b"%PDF-1.4\n")
    assert "c.pdf" in [f.filename for f in discover_files(raw_dir)]


def test_missing_directory_yields_nothing(tmp_path):
    assert list(discover_files(tmp_path / "does-not-exist")) == []


def test_describe_captures_a_content_hash(raw_dir):
    a, b = describe(raw_dir / "a.pdf"), describe(raw_dir / "b.pdf")
    assert len(a.sha256) == 64
    assert a.sha256 != b.sha256
    assert a.size_bytes == len(b"%PDF-1.4 first\n")


def test_batch_ids_are_filename_safe_and_sort_chronologically():
    early = new_batch_id(datetime(2026, 9, 9, 8, 0, 0, tzinfo=timezone.utc))
    late = new_batch_id(datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc))
    assert early < late
    assert not set(early) & set('\\/:*?"<>|')


# S3 staging
class FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((path, bucket, key, ExtraArgs))


def test_keys_group_a_batch_together(pipeline_env):
    uploader = S3Uploader(client=FakeS3())
    assert uploader.key_for("a.pdf", "b1") == "incoming/b1/a.pdf"


def test_staging_uploads_with_a_pdf_content_type(raw_dir, pipeline_env):
    fake = FakeS3()
    staged = S3Uploader(client=fake).stage(raw_dir / "a.pdf", "b1")
    path, bucket, key, extra = fake.uploads[0]
    assert bucket == "test-bucket"
    assert key == "incoming/b1/a.pdf"
    assert extra == {"ContentType": "application/pdf"}
    assert staged.uri == "s3://test-bucket/incoming/b1/a.pdf"


def test_staged_object_renders_the_textract_document_location():
    staged = StagedObject(bucket="b", key="incoming/b1/a.pdf")
    assert staged.as_textract_document() == {
        "S3Object": {"Bucket": "b", "Name": "incoming/b1/a.pdf"}
    }


def test_uploader_does_not_need_credentials_to_construct(pipeline_env):
    """Lazy client construction keeps import and object construction credential-free."""
    uploader = S3Uploader()
    assert uploader.bucket == "test-bucket"
    assert uploader._client is None
