"""`run_s3_batch()` end to end: discovery from a fake S3 date partition,
straight through extraction/mapping/validation/storage. Extraction/mapping
are routed through fixture data (`FixtureTextractClient`/
`FixtureBedrockMapper`, `conftest.py`) and storage runs against a real,
moto-mocked DynamoDB table -- proving the S3-sourced path produces the same
outcomes as the local-file path (`test_pipeline.py`) for the same underlying
sample, with realistic AWS semantics on both the S3 and DynamoDB legs, and
without spending on real Textract/Bedrock for every test run.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src import settings
from src.pipeline.run_batch import (
    LOADED_WITH_WARNINGS,
    QUARANTINED,
    Dependencies,
    run_s3_batch,
)
from src.storage.dynamo_client import DynamoStore, table_definition
from tests.conftest import FixtureBedrockMapper, FixtureTextractClient

BUCKET = "invoice-pipe"
BATCH_DATE = date(2026, 5, 14)
PREFIX = "2026/05/14"
TABLE = "invoice_records_test"


@pytest.fixture
def s3(pipeline_env, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-2")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
        )
        yield client


@pytest.fixture
def seeded(s3, pipeline_env):
    """Puts real PDF bytes at s3://invoice-pipe/2026/05/14/<file>.pdf --
    discovery reads this for real; extraction/mapping are fixture-routed via
    the `deps` fixture, keyed by filename stem, not by anything read from S3."""
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/Invoice1653194348.pdf",
        Body=b"%PDF-1.4 stub\n",
    )
    return pipeline_env


@pytest.fixture
def deps(s3, pipeline_env, monkeypatch, textract_response, mapped_fixture):
    """Depends on `s3` to reuse its already-active `mock_aws()` context
    moto mocks every AWS service at once, so no second `with mock_aws():`
    block is needed to also cover DynamoDB here."""
    resource = boto3.resource("dynamodb", region_name="eu-west-2")
    resource.create_table(**table_definition(TABLE))
    responses = {"Invoice1653194348": textract_response("Invoice1653194348")}
    mapped = {"Invoice1653194348": mapped_fixture("Invoice1653194348")}
    return Dependencies(
        extractor=FixtureTextractClient(responses),
        mapper=FixtureBedrockMapper(mapped),
        store=DynamoStore(table_name=TABLE, resource=resource),
    )


def test_s3_sourced_file_loads_with_nothing_pre_staged_locally(seeded, deps, s3):
    """The whole point: processing reads straight from S3, needing no local
    copy to exist beforehand, whatever mirror_local does with a *copy*
    afterward is a separate, secondary concern (see the mirroring tests
    below)."""
    assert list(settings.data_path("raw").iterdir()) == []
    result = run_s3_batch(
        bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3
    )
    assert len(result.outcomes) == 1
    assert result.outcomes[0].outcome == LOADED_WITH_WARNINGS
    assert result.outcomes[0].filename == "Invoice1653194348.pdf"


def test_record_shape_matches_the_local_flow_exactly(seeded, deps, s3):
    """Discovery source shouldn't leak into the stored record at all."""
    run_s3_batch(bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3)
    item = next(i for i in deps.store.scan_all() if i.get("PK") == "VENDOR#GYMLOUNGE")
    assert item["total_amount"] == Decimal("48.00")
    assert item["SK"] == "INVOICE#GYMLOUNGE20222023240"


def test_no_upload_happens_for_an_s3_sourced_file(seeded, deps, s3):
    """No re-upload: the object at the original key is untouched, and no
    second copy appears under an incoming/ staging prefix."""
    run_s3_batch(bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == [f"{PREFIX}/Invoice1653194348.pdf"]  # exactly the original


def test_a_different_days_partition_is_empty(seeded, deps, s3):
    result = run_s3_batch(
        bucket=BUCKET, batch_date=date(2026, 5, 15), deps=deps, batch_id="b1", s3_client=s3
    )
    assert result.outcomes == []


def test_bad_pdf_bytes_in_s3_are_quarantined_not_skipped(seeded, deps, s3):
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/fake.pdf", Body=b"definitely not a pdf")
    result = run_s3_batch(
        bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3
    )
    outcome = next(o for o in result.outcomes if o.filename == "fake.pdf")
    assert outcome.outcome == QUARANTINED
    assert "not a PDF" in outcome.error_message

    sidecar = settings.data_path("quarantine") / "fake.pdf.error.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["source_file"] == f"s3://{BUCKET}/{PREFIX}/fake.pdf"


def test_rerunning_the_same_day_skips_duplicates(seeded, deps, s3):
    run_s3_batch(bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3)
    second = run_s3_batch(
        bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b2", s3_client=s3
    )
    assert second.outcomes[0].outcome == "DUPLICATE_SKIPPED"


def test_explicit_batch_date_beats_the_configured_override(seeded, deps, s3):
    """settings.yaml's batch_date_override is 2026-05-14 in this fixture
    tree; passing a different date here must win, proving the override is a
    default, not a hardcoded assumption baked into the code path."""
    s3.put_object(
        Bucket=BUCKET, Key="2026/05/20/Invoice1653194348.pdf", Body=b"%PDF-1.4\n"
    )
    result = run_s3_batch(
        bucket=BUCKET, batch_date=date(2026, 5, 20), deps=deps, batch_id="b1", s3_client=s3
    )
    assert len(result.outcomes) == 1


# silver output lands at the date-partitioned path, mirroring data/raw
def test_silver_output_is_date_partitioned_for_s3_sourced_batches(seeded, deps, s3):
    run_s3_batch(bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3)
    silver = settings.data_path("silver")
    assert (silver / "2026" / "5" / "14" / "Invoice1653194348.textract.json").exists()
    assert (silver / "2026" / "5" / "14" / "Invoice1653194348.mapped.json").exists()


def test_local_flow_silver_stays_flat_unaffected_by_partitioning(seeded, deps, s3, tmp_path):
    """The local-file flow (run_batch, not run_s3_batch) has no batch date at
    all which confirms date-partitioning is additive, not a regression for the
    existing local path."""
    from src.pipeline.run_batch import run_batch

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "Invoice1653194348.pdf").write_bytes(b"%PDF-1.4\n")
    run_batch(raw_dir=raw, deps=deps, batch_id="local1")
    silver = settings.data_path("silver")
    assert (silver / "Invoice1653194348.textract.json").exists()
    assert not (silver / "2026").exists()


# local mirroring: on by default, opt-out available, never blocks processing
def test_mirror_local_defaults_to_on_and_populates_data_raw(seeded, deps, s3):
    run_s3_batch(bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3)
    mirrored = settings.data_path("raw") / "2026" / "5" / "14" / "Invoice1653194348.pdf"
    assert mirrored.exists()
    assert mirrored.read_bytes() == b"%PDF-1.4 stub\n"


def test_mirror_local_false_leaves_data_raw_empty(seeded, deps, s3):
    run_s3_batch(
        bucket=BUCKET,
        batch_date=BATCH_DATE,
        deps=deps,
        batch_id="b1",
        s3_client=s3,
        mirror_local=False,
    )
    assert list(settings.data_path("raw").iterdir()) == []


def test_mirror_failure_does_not_block_processing(seeded, deps, s3, monkeypatch, caplog):
    """Processing reads from S3 directly, not from the mirror, a mirror
    failure is a local convenience gone wrong, not a reason to lose the batch."""
    import logging

    from src.ingestion import s3_discovery

    def exploding_mirror(*args, **kwargs):
        raise RuntimeError("disk full, or whatever went wrong locally")

    monkeypatch.setattr(s3_discovery, "mirror_to_local", exploding_mirror)
    # run_batch.py imported as the name directly, so the patch has to land there too.
    import src.pipeline.run_batch as run_batch_mod

    monkeypatch.setattr(run_batch_mod, "mirror_to_local", exploding_mirror)

    with caplog.at_level(logging.WARNING):
        result = run_s3_batch(
            bucket=BUCKET, batch_date=BATCH_DATE, deps=deps, batch_id="b1", s3_client=s3
        )

    assert result.outcomes[0].outcome == LOADED_WITH_WARNINGS  # still processed
    assert any("mirror" in r.message for r in caplog.records)  # failure was logged
    assert list(settings.data_path("raw").iterdir()) == []  # no partial mirror either


def test_mirror_local_respects_a_custom_local_root(seeded, deps, s3, tmp_path):
    custom_root = tmp_path / "elsewhere"
    run_s3_batch(
        bucket=BUCKET,
        batch_date=BATCH_DATE,
        deps=deps,
        batch_id="b1",
        s3_client=s3,
        local_root=custom_root,
    )
    assert (custom_root / "2026" / "5" / "14" / "Invoice1653194348.pdf").exists()
    assert list(settings.data_path("raw").iterdir()) == []  # default location untouched

# service-limit pre-check: rejected before the extractor is ever called
def test_oversized_file_never_reaches_the_extractor(pipeline_env):
    """process_one() is the shared entry both run_batch() and run_s3_batch()
    funnel through testing it directly, with a hand-built oversized
    S3DiscoveredFile, is more direct than staging 500MB+ into a fake bucket."""
    from datetime import date as date_

    from src.extraction.textract_client import MAX_DOCUMENT_SIZE_BYTES
    from src.ingestion.s3_discovery import S3DiscoveredFile
    from src.pipeline.errors import TextractExtractionError
    from src.pipeline.run_batch import Dependencies, process_one

    class ExplodingExtractor:
        def analyze(self, staged, source_file):
            raise AssertionError("Textract must never be called for an oversized file")

    huge = S3DiscoveredFile(
        bucket=BUCKET,
        key=f"{PREFIX}/huge.pdf",
        filename="huge.pdf",
        size_bytes=MAX_DOCUMENT_SIZE_BYTES + 1,
        last_modified_utc="2026-05-14T00:00:00+00:00",
        has_pdf_magic=True,
        batch_date=date_(2026, 5, 14),
    )
    # store=None is safe here: check_document_size() raises before process_one()
    # ever reaches staging or storage.
    deps = Dependencies(extractor=ExplodingExtractor(), mapper=None, store=None)

    with pytest.raises(TextractExtractionError, match="exceeds Textract's"):
        process_one(huge, deps, "b1")
