"""Date-partitioned S3 discovery: prefix rendering, date resolution, listing.
    Very Important for organising files correctly"""

from __future__ import annotations

from datetime import date, datetime, timezone

import boto3
import pytest
from moto import mock_aws

from src.ingestion.s3_discovery import (
    DEFAULT_PREFIX_TEMPLATE,
    S3DiscoveredFile,
    batch_date_prefix,
    discover_s3_files,
    mirror_to_local,
    resolve_batch_date,
)

BUCKET = "invoice-pipe"


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


def put(client, key: str, body: bytes = b"%PDF-1.4 stub\n"):
    client.put_object(Bucket=BUCKET, Key=key, Body=body)


# prefix rendering
def test_default_template_zero_pads():
    # Explicit template, not the fallback: without pipeline_env sandboxing,
    # this call would otherwise silently read the live config/settings.yaml
    # (which the project is free to override) instead of pinning down the
    # module's own DEFAULT_PREFIX_TEMPLATE constant, which is this test's point.
    assert (
        batch_date_prefix(date(2026, 5, 14), template=DEFAULT_PREFIX_TEMPLATE)
        == "2026/05/14/"
    )


def test_zero_padding_matters_for_lexicographic_month_order():
    """Unpadded '5' sorts after '10' as a string -- exactly the bug zero
    padding exists to avoid for any future date-range prefix scan."""
    october = batch_date_prefix(date(2026, 10, 1), template="{year}/{month}/{day}/")
    may = batch_date_prefix(date(2026, 5, 1), template="{year}/{month}/{day}/")
    assert sorted([october, may])[0] == october  # "10" < "5" lexicographically


def test_custom_unpadded_template_is_honored():
    assert (
        batch_date_prefix(date(2026, 5, 14), template="{year}/{month}/{day}/")
        == "2026/5/14/"
    )


def test_prefix_template_is_configurable_via_settings(pipeline_env):
    import yaml

    from src import settings

    data = yaml.safe_load(settings.settings_path().read_text(encoding="utf-8"))
    data["aws"]["s3_raw_prefix_template"] = "{year}-{month:02d}-{day:02d}/"
    settings.settings_path().write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.reset_cache()

    assert batch_date_prefix(date(2026, 5, 14)) == "2026-05-14/"


# batch date resolution
def test_explicit_date_object_wins(pipeline_env):
    assert resolve_batch_date(date(2020, 1, 1)) == date(2020, 1, 1)


def test_explicit_string_is_parsed(pipeline_env):
    assert resolve_batch_date("2026-05-14") == date(2026, 5, 14)


def test_falls_back_to_config_override(pipeline_env):
    """pipeline_env's settings fixture sets no override, so this proves that the
    fallback chain reaches real 'today' rather than silently defaulting to
    some other fixed date when nothing is configured. That is how it should be."""
    assert resolve_batch_date() == datetime.now(timezone.utc).date()


def test_config_override_beats_today(pipeline_env):
    import yaml

    from src import settings

    data = yaml.safe_load(settings.settings_path().read_text(encoding="utf-8"))
    data["pipeline"]["batch_date_override"] = "2026-05-14"
    settings.settings_path().write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.reset_cache()

    assert resolve_batch_date() == date(2026, 5, 14)


def test_explicit_argument_beats_config_override(pipeline_env):
    import yaml

    from src import settings

    data = yaml.safe_load(settings.settings_path().read_text(encoding="utf-8"))
    data["pipeline"]["batch_date_override"] = "2026-05-14"
    settings.settings_path().write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.reset_cache()

    assert resolve_batch_date("2030-01-01") == date(2030, 1, 1)


# 
# listing against a fake S3 (moto), instead of boto, no real AWS touched
def test_lists_only_pdfs_from_the_requested_day(s3):
    put(s3, "2026/05/14/Invoice1653194348.pdf")
    put(s3, "2026/05/14/2324GBRAMD125920.pdf")
    put(s3, "2026/05/14/readme.txt", body=b"not a pdf")
    put(s3, "2026/05/13/OtherDay.pdf")  # different day, must not appear

    found = sorted(
        f.filename
        for f in discover_s3_files(BUCKET, date(2026, 5, 14), client=s3)
    )
    assert found == ["2324GBRAMD125920.pdf", "Invoice1653194348.pdf"]


def test_empty_partition_yields_nothing(s3):
    assert list(discover_s3_files(BUCKET, date(2026, 5, 14), client=s3)) == []


def test_results_are_sorted_for_reproducible_batches(s3):
    put(s3, "2026/05/14/zzz.pdf")
    put(s3, "2026/05/14/aaa.pdf")
    put(s3, "2026/05/14/mmm.pdf")
    found = [f.filename for f in discover_s3_files(BUCKET, date(2026, 5, 14), client=s3)]
    assert found == ["aaa.pdf", "mmm.pdf", "zzz.pdf"]


def test_bucket_and_date_default_from_settings(s3, pipeline_env):
    """No bucket/date args, as both come from config, same as the CLI's
    plain `--from-s3` with no overrides which would resolve them."""
    import yaml

    from src import settings

    data = yaml.safe_load(settings.settings_path().read_text(encoding="utf-8"))
    data["aws"]["s3_raw_bucket"] = BUCKET
    data["pipeline"]["batch_date_override"] = "2026-05-14"
    settings.settings_path().write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.reset_cache()

    put(s3, "2026/05/14/Invoice1653194348.pdf")
    found = list(discover_s3_files(client=s3))
    assert [f.filename for f in found] == ["Invoice1653194348.pdf"]


def test_real_magic_bytes_are_detected(s3):
    put(s3, "2026/05/14/good.pdf", body=b"%PDF-1.7\n...")
    put(s3, "2026/05/14/bad.pdf", body=b"this is not a pdf at all")
    by_name = {f.filename: f for f in discover_s3_files(BUCKET, date(2026, 5, 14), client=s3)}
    assert by_name["good.pdf"].has_pdf_magic is True
    assert by_name["bad.pdf"].has_pdf_magic is False


def test_verify_magic_false_skips_the_extra_call(s3):
    put(s3, "2026/05/14/bad.pdf", body=b"not actually a pdf")
    found = list(
        discover_s3_files(BUCKET, date(2026, 5, 14), client=s3, verify_magic=False)
    )
    assert found[0].has_pdf_magic is True  # unverified, so assumed fine


def test_s3_discovered_file_exposes_stem_and_uri(s3):
    put(s3, "2026/05/14/Invoice1653194348.pdf")
    f = next(iter(discover_s3_files(BUCKET, date(2026, 5, 14), client=s3)))
    assert f.stem == "Invoice1653194348"
    assert f.uri == f"s3://{BUCKET}/2026/05/14/Invoice1653194348.pdf"


# mirroring S3-discovered files into a matching local directory structur
def test_mirror_lands_at_local_root_year_month_day_filename(s3, tmp_path):
    """The bucket's own 'raw/' root is not reproduced locally -- local_root
    (data/raw) already means 'raw layer', so the mirror is just the date."""
    put(s3, "raw/2026/5/14/Order_ID_7104598035.pdf", body=b"%PDF-1.4 real bytes\n")
    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))

    written = mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)

    dest = tmp_path / "2026" / "5" / "14" / "Order_ID_7104598035.pdf"
    assert written == [dest]
    assert dest.exists()
    assert dest.read_bytes() == b"%PDF-1.4 real bytes\n"


def test_mirror_is_independent_of_the_buckets_own_prefix_layout(s3, tmp_path):
    """Same destination regardless of what precedes the date in the S3 key --
    a bucket with no 'raw/' root, or a differently-named one, mirrors to the
    identical local path."""
    put(s3, "incoming/other-root/2026/5/14/x.pdf", body=b"%PDF-1.4\n")
    files = list(
        discover_s3_files(
            BUCKET,
            date(2026, 5, 14),
            client=s3,
            prefix_template="incoming/other-root/{year}/{month}/{day}/",
        )
    )
    written = mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)
    assert written == [tmp_path / "2026" / "5" / "14" / "x.pdf"]


def test_mirror_creates_intermediate_directories(s3, tmp_path):
    put(s3, "raw/2026/5/14/x.pdf")
    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))
    mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)
    assert (tmp_path / "2026" / "5" / "14").is_dir()


def test_mirror_skips_a_file_already_present_at_the_same_size(s3, tmp_path):
    body = b"%PDF-1.4 unchanged\n"
    put(s3, "raw/2026/5/14/x.pdf", body=body)
    dest = tmp_path / "2026" / "5" / "14" / "x.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(body)
    dest_mtime_before = dest.stat().st_mtime_ns

    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))
    written = mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)

    assert written == []  # nothing re-downloaded
    assert dest.stat().st_mtime_ns == dest_mtime_before  # untouched


def test_mirror_redownloads_a_file_whose_size_changed(s3, tmp_path):
    dest = tmp_path / "2026" / "5" / "14" / "x.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"stale, wrong size")

    put(s3, "raw/2026/5/14/x.pdf", body=b"%PDF-1.4 the real, current file\n")
    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))
    written = mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)

    assert written == [dest]
    assert dest.read_bytes() == b"%PDF-1.4 the real, current file\n"


def test_mirror_handles_multiple_files_independently(s3, tmp_path):
    put(s3, "raw/2026/5/14/a.pdf", body=b"aaa")
    put(s3, "raw/2026/5/14/b.pdf", body=b"bbbb")
    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))
    written = mirror_to_local(files, date(2026, 5, 14), local_root=tmp_path, client=s3)
    assert {p.name for p in written} == {"a.pdf", "b.pdf"}


def test_mirror_defaults_local_root_to_configured_raw_path(s3, pipeline_env):
    put(s3, "raw/2026/5/14/x.pdf", body=b"%PDF-1.4\n")
    files = list(discover_s3_files(
        BUCKET, date(2026, 5, 14), client=s3,
        prefix_template="raw/{year}/{month}/{day}/",
    ))
    written = mirror_to_local(files, date(2026, 5, 14), client=s3)  # no local_root
    assert written[0] == pipeline_env / "data" / "raw" / "2026" / "5" / "14" / "x.pdf"
