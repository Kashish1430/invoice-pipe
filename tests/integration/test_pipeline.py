"""End-to-end batch behaviour: per-file isolation, quarantine, idempotency.

Runs the real orchestrator over the six real sample invoices plus one
explicitly synthetic document, with extraction/mapping routed through
fixture data (`FixtureTextractClient`/`FixtureBedrockMapper`,
`conftest.py`) and storage against a real, moto-mocked DynamoDB table, so
per-file isolation, quarantine, dedup, storage, and the gold mirror are all
exercised together against realistic AWS semantics, without spending on
real Textract/Bedrock calls for every test run. Extraction/mapping call
mechanics (polling, pagination, forced tool use, retries) are covered
directly in `test_textract_mock.py` / `test_bedrock_mock.py`; this file is
about batch-level orchestration behavior.

`_negative_total_credit_note` is the one deliberately synthetic addition:
all six real samples now load cleanly once run against real AWS (see
`tests/fixtures/README.md`), so nothing in this batch would otherwise
exercise the quarantine path at the orchestration level, sidecar writing,
tombstoning, and "one bad file doesn't abort the batch" all need a document
that actually hard-fails, and none of the real ones do.

The source PDFs are stubbed to a `%PDF-` header: nothing downstream of the
fixture-routed extractor reads their bytes, and keeping the real ones out of
the temp tree keeps the test fast.
"""

from __future__ import annotations

import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src import settings
from src.pipeline.run_batch import (
    DUPLICATE_SKIPPED,
    LOADED,
    LOADED_WITH_WARNINGS,
    QUARANTINED,
    Dependencies,
    run_batch,
)
from src.stats import batch_stats
from src.storage.dynamo_client import DynamoStore, table_definition
from tests.conftest import FixtureBedrockMapper, FixtureTextractClient

SAMPLES = [
    "2324GBRAMD125920",
    "7042968270_543523577_4_2026",
    "E-Receipt (2)",
    "Invoice1653194348",
    "Order_ID_7104598035",
    "SALES RECEIPT_304743_1750688634308",
    "_negative_total_credit_note",  # synthetic -- see module docstring
]

TABLE = "invoice_records_test"


@pytest.fixture
def seeded(pipeline_env):
    """Stub PDFs in raw/ so local discovery has something to find."""
    raw = settings.data_path("raw")
    for stem in SAMPLES:
        (raw / f"{stem}.pdf").write_bytes(b"%PDF-1.4 stub\n")
    return pipeline_env


@pytest.fixture
def deps(pipeline_env, monkeypatch, textract_response, mapped_fixture):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-2")
        resource.create_table(**table_definition(TABLE))
        responses = {stem: textract_response(stem) for stem in SAMPLES}
        mapped = {stem: mapped_fixture(stem) for stem in SAMPLES}
        yield Dependencies(
            extractor=FixtureTextractClient(responses),
            mapper=FixtureBedrockMapper(mapped),
            store=DynamoStore(table_name=TABLE, resource=resource),
        )


# the batch as a whole
def test_batch_processes_every_file(seeded, deps):
    result = run_batch(deps=deps, batch_id="b1")
    assert len(result.outcomes) == 7
    assert result.counts() == {
        LOADED: 3,               # E-Receipt, Order_ID, SALES RECEIPT -- all real, all clean
        LOADED_WITH_WARNINGS: 3,  # VFS, Airtel, Gym Lounge
        QUARANTINED: 1,           # the one synthetic document
    }


def test_one_bad_file_does_not_abort_the_batch(seeded, deps):
    """The synthetic credit note hard-fails but the six real files still load."""
    result = run_batch(deps=deps, batch_id="b1")
    by_name = {o.filename: o for o in result.outcomes}
    assert by_name["_negative_total_credit_note.pdf"].outcome == QUARANTINED
    assert by_name["Invoice1653194348.pdf"].outcome == LOADED_WITH_WARNINGS
    assert len(deps.store.scan_all()) == 7  # 6 records + 1 quarantine tombstone


def test_flags_across_the_batch(seeded, deps):
    """All six real documents were checked against real AWS end to end
    (docs/challenges.md number 18 and 19) and load clean or with the flags below --
    none of them are ambiguous-date cases; that was only ever true of the
    synthetic content the Order_ID_7104598035 fixture used to hold."""
    result = run_batch(deps=deps, batch_id="b1")
    flags = result.flag_counts()
    assert flags["ZERO_TOTAL_AMOUNT"] == 1          # VFS Grand Total 0.00
    assert flags["ZERO_TAX_WITH_NONZERO_RATE"] == 1  # Gym Lounge 9% on 0.00
    assert flags["LOW_FIELD_CONFIDENCE"] == 1        # VFS garbled buyer name
    assert flags.get("AMBIGUOUS_DATE_FORMAT", 0) == 0
    assert flags["MULTIPLE_TOTAL_CANDIDATES"] == 3


def test_gym_lounge_record_matches_the_worked_example(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    item = next(
        i for i in deps.store.scan_all() if i.get("PK") == "VENDOR#GYMLOUNGE"
    )
    assert item["SK"] == "INVOICE#GYMLOUNGE20222023240"
    assert item["total_amount"] == Decimal("48.00")     # 6000 INR / 125
    assert item["original_total_amount"] == Decimal("6000.00")
    assert item["original_currency"] == "INR"
    assert item["currency"] == "GBP"
    assert item["GSI1PK"] == "DATE#2022-05"
    assert item["status"] == "LOADED_WITH_WARNINGS"
    assert item["unmapped_metadata"]["gst_no"] == "null"


def test_airtel_takes_the_amount_payable_not_a_competing_total(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    item = next(
        i for i in deps.store.scan_all() if i.get("PK") == "VENDOR#BHARTIAIRTELLIMITED"
    )
    assert item["original_total_amount"] == Decimal("805.82")
    assert item["original_product_amount"] == Decimal("698.00")
    assert item["original_vat_tax_amount"] == Decimal("107.82")


# quarantine
def test_quarantine_writes_a_sidecar_and_leaves_the_pdf_alone(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    name = "_negative_total_credit_note.pdf"
    sidecar = settings.data_path("quarantine") / f"{name}.error.json"
    assert sidecar.exists()
    assert (settings.data_path("raw") / name).exists()  # raw is immutable

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["failed_at_stage"] == "pydantic_validation"
    assert payload["dq_rule"] == "NEGATIVE_MONEY_VALUE"
    assert payload["batch_id"] == "b1"
    # the pre-Pydantic dict, so the failure is debuggable without a re-run
    assert payload["raw_mapped_payload"]["fields"]["total_amount"] == "-1,250.00"


def test_quarantined_file_leaves_a_status_tombstone(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    rows = deps.store.query_status("QUARANTINED")
    assert len(rows) == 1
    assert rows[0]["source_file"] == "_negative_total_credit_note.pdf"


def test_a_non_pdf_is_quarantined_not_skipped(seeded, deps):
    (settings.data_path("raw") / "notes.pdf").write_bytes(b"just some text")
    result = run_batch(deps=deps, batch_id="b1")
    outcome = next(o for o in result.outcomes if o.filename == "notes.pdf")
    assert outcome.outcome == QUARANTINED
    assert "not a PDF" in outcome.error_message


def test_a_file_with_no_fixture_is_quarantined(seeded, deps):
    (settings.data_path("raw") / "unseen.pdf").write_bytes(b"%PDF-1.4\n")
    result = run_batch(deps=deps, batch_id="b1")
    outcome = next(o for o in result.outcomes if o.filename == "unseen.pdf")
    assert outcome.outcome == QUARANTINED
    assert outcome.error_type == "TextractExtractionError"


# idempotency
def test_rerunning_the_same_batch_skips_duplicates(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    second = run_batch(deps=deps, batch_id="b2")
    assert second.counts()[DUPLICATE_SKIPPED] == 6
    assert second.counts()[QUARANTINED] == 1  # still hard-fails, still no record


def test_duplicates_are_not_quarantined(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    second = run_batch(deps=deps, batch_id="b2")
    duplicate = next(o for o in second.outcomes if o.outcome == DUPLICATE_SKIPPED)
    sidecar = settings.data_path("quarantine") / f"{duplicate.filename}.error.json"
    assert not sidecar.exists()

# gold mirror and stats

def test_gold_mirrors_what_was_loaded(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    lines = (settings.data_path("gold") / "b1.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 6  # the quarantined file is not mirrored
    keys = {json.loads(line)["dedup_key"] for line in lines}
    assert "GYMLOUNGE|GYMLOUNGE20222023240" in keys


def test_stats_can_be_computed_from_the_gold_mirror(seeded, deps):
    """Offline batches leave no table to scan, so gold is the aggregate source.

    The mirror holds loads only, so quarantines show as zero there, the
    tombstones live in storage, not in gold.
    """
    run_batch(deps=deps, batch_id="b1")
    stats = batch_stats.compute(
        batch_stats.load_gold(settings.data_path("gold") / "b1.jsonl")
    )
    assert stats.invoice_records == 6
    assert stats.quarantine_tombstones == 0
    assert stats.total_gbp == Decimal("683.34")
    assert isinstance(stats.total_gbp, Decimal)


def test_batch_stats_aggregate_the_loaded_batch(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    stats = batch_stats.collect(deps.store)
    assert stats.invoice_records == 6
    assert stats.quarantine_tombstones == 1
    assert stats.by_status["LOADED"] == 3
    assert stats.by_status["LOADED_WITH_WARNINGS"] == 3
    assert stats.dq_flag_counts["MULTIPLE_TOTAL_CANDIDATES"] == 3
    # 2 of the 6 real samples turned out to be GBP-native (Trip.com, Sephra),
    # not INR like the brief's original premise -- see README "known gaps".
    assert stats.by_original_currency == {"INR": 4, "GBP": 2}
    assert stats.clean_rate == pytest.approx(0.5)
    assert not stats.exceeds_migration_threshold


def test_month_scoped_stats_use_the_date_index(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    stats = batch_stats.collect(deps.store, "2022-05")
    assert stats.invoice_records == 1


def test_stats_report_lists_every_soft_rule_including_zeroes(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    text = batch_stats.report(batch_stats.collect(deps.store))
    for rule in batch_stats.known_flags():
        assert rule in text


# silver persistence -- audit/debug artifact, written on every real run
def test_silver_is_persisted_for_every_processed_file(seeded, deps):
    run_batch(deps=deps, batch_id="b1")
    silver = settings.data_path("silver")
    for stem in SAMPLES:
        assert (silver / f"{stem}.textract.json").exists()
        assert (silver / f"{stem}.mapped.json").exists()


def test_limit_stops_early(seeded, deps):
    assert len(run_batch(deps=deps, batch_id="b1", limit=2).outcomes) == 2


def test_run_batch_accepts_an_explicit_raw_dir(seeded, deps, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "Invoice1653194348.pdf").write_bytes(b"%PDF-1.4\n")
    result = run_batch(raw_dir=other, deps=deps, batch_id="b1")
    assert [o.filename for o in result.outcomes] == ["Invoice1653194348.pdf"]


def test_empty_raw_dir_yields_an_empty_batch(pipeline_env, deps):
    assert run_batch(deps=deps, batch_id="b1").outcomes == []
