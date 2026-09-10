"""Discovers PDFs already sitting in S3, partitioned by business date.

Unlike `file_discovery.discover_files()` -- which walks a local directory that
still needs staging to S3 before Textract can read it -- this module treats S3
itself as the source of truth: PDFs live permanently at
`s3://<bucket>/<year>/<month>/<day>/<file>.pdf`, uploaded there by whatever
process lands them (a daily ingestion job, a manual console upload). Discovery
for a given day is one `list_objects_v2` call under that day's prefix, and the
results go straight into `process_one()` with no re-upload step -- the file is
already exactly where Textract needs it.

The batch date is a business-date concept, not "whenever this code happened to
run": production code calling `resolve_batch_date()` with no argument gets
`date.today()`, and `pipeline.batch_date_override` in settings.yaml (or the
`--batch-date` CLI flag on `run_batch`) exists to reprocess or demo a specific
day's partition regardless of the wall clock -- the same override any
daily-batch system needs for backfills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import boto3
from botocore.exceptions import ClientError

from src import settings

_PDF_MAGIC = b"%PDF-"
DEFAULT_PREFIX_TEMPLATE = "{year}/{month:02d}/{day:02d}/"


@dataclass(frozen=True)
class S3DiscoveredFile:
    """The S3-sourced counterpart to `file_discovery.DiscoveredFile`.

    No local `path`: the file's only home is S3, referenced by bucket + key.
    `process_one()` (`run_batch.py`) checks `isinstance(discovered,
    S3DiscoveredFile)` to skip the local-upload staging step entirely --
    there is nothing to upload, the file is already there.
    """

    bucket: str
    key: str
    filename: str
    size_bytes: int
    last_modified_utc: str
    has_pdf_magic: bool
    batch_date: date_

    @property
    def stem(self) -> str:
        return self.filename[:-4] if self.filename.lower().endswith(".pdf") else self.filename

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def resolve_batch_date(explicit: date_ | str | None = None) -> date_:
    """The business date a batch processes.

    Precedence: an explicit argument, then `pipeline.batch_date_override` in
    settings, then the real wall-clock date. The override is a normal
    backfill/reprocessing lever, not a claim about when this code was written
    -- production code that never passes `explicit` and never sets the
    override gets today's real date, every time.
    """
    if isinstance(explicit, date_):
        return explicit
    if isinstance(explicit, str) and explicit:
        return datetime.strptime(explicit, "%Y-%m-%d").date()
    override = settings.get("pipeline.batch_date_override", None)
    if override:
        return datetime.strptime(str(override), "%Y-%m-%d").date()
    return datetime.now(timezone.utc).date()


def batch_date_prefix(batch_date: date_, template: str | None = None) -> str:
    """Render the S3 prefix for one day, e.g. `2026/05/14/`.

    Zero-padded by default (`{month:02d}`) so prefixes sort correctly as
    plain strings -- an unpadded `5/` sorts *after* `10/` lexicographically,
    which silently breaks any future date-range prefix scan. Override via
    `aws.s3_raw_prefix_template` only if the bucket genuinely uses unpadded
    folder names.
    """
    template = template or settings.get(
        "aws.s3_raw_prefix_template", DEFAULT_PREFIX_TEMPLATE
    )
    return template.format(
        year=batch_date.year, month=batch_date.month, day=batch_date.day
    )


def _has_pdf_magic(client, bucket: str, key: str) -> bool:
    try:
        response = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-4")
        return response["Body"].read() == _PDF_MAGIC
    except ClientError:
        # A failed range-read shouldn't hide the file from the batch; let
        # extraction fail loudly and quarantine it, rather than discovery
        # silently dropping it from the run.
        return True


def discover_s3_files(
    bucket: str | None = None,
    batch_date: date_ | str | None = None,
    *,
    client=None,
    prefix_template: str | None = None,
    verify_magic: bool = True,
) -> Iterator[S3DiscoveredFile]:
    """List every PDF under one day's partition, sorted by key.

    Sorted for the same reason `file_discovery.discover_files()` sorts: two
    runs against the same day's partition process files in the same order.
    """
    bucket = bucket or settings.get("aws.s3_raw_bucket")
    client = client or boto3.client("s3", region_name=settings.get("aws.region"))
    resolved_date = resolve_batch_date(batch_date)
    prefix = batch_date_prefix(resolved_date, prefix_template)

    rows: list[tuple[str, int, str]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue
            last_modified = obj.get("LastModified")
            lm_iso = (
                last_modified.astimezone(timezone.utc).isoformat()
                if hasattr(last_modified, "astimezone")
                else str(last_modified)
            )
            rows.append((key, int(obj.get("Size", 0)), lm_iso))

    for key, size, last_modified_utc in sorted(rows):
        filename = key.rsplit("/", 1)[-1]
        magic_ok = _has_pdf_magic(client, bucket, key) if verify_magic else True
        yield S3DiscoveredFile(
            bucket=bucket,
            key=key,
            filename=filename,
            size_bytes=size,
            last_modified_utc=last_modified_utc,
            has_pdf_magic=magic_ok,
            batch_date=resolved_date,
        )


def mirror_to_local(
    files: Iterable[S3DiscoveredFile],
    batch_date: date_,
    local_root: str | Path | None = None,
    *,
    client=None,
) -> list[Path]:
    """Download S3-discovered files into `<local_root>/<year>/<month>/<day>/`.

    Deliberately rebuilt from `batch_date`, not the S3 key verbatim: whatever
    folder wraps the date in the bucket (a `raw/` root, an `incoming/` root,
    nothing at all) is that bucket's own layout choice, not something the
    local mirror should inherit -- `<local_root>` (normally `data/raw`)
    already means "this is the raw layer" on the local side. The result is
    always `<local_root>/<year>/<month>/<day>/<filename>`, unpadded, matching
    what a plain `date.year` / `.month` / `.day` render as -- local discovery
    (`file_discovery.discover_files()`) recurses via `rglob` regardless of
    padding, so nothing downstream cares.

    Idempotent: a file already present locally at the same size is not
    re-downloaded, so re-running this against an unchanged partition costs
    nothing beyond one stat check per file.
    """
    local_root = (
        Path(local_root) if local_root is not None else settings.data_path("raw")
    )
    client = client or boto3.client("s3", region_name=settings.get("aws.region"))
    date_dir = local_root / str(batch_date.year) / str(batch_date.month) / str(batch_date.day)

    written: list[Path] = []
    for f in files:
        dest = date_dir / f.filename
        if dest.exists() and dest.stat().st_size == f.size_bytes:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(f.bucket, f.key, str(dest))
        written.append(dest)
    return written
