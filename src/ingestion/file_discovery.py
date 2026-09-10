"""Walks `data/raw` and yields the PDFs a batch should process.

`data/raw` is immutable: nothing here moves, renames or deletes a source file,
including on failure. A quarantined document is recorded by a sidecar in
`data/quarantine`, never by relocating the evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from src import settings

_PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    filename: str
    size_bytes: int
    sha256: str
    modified_utc: str
    has_pdf_magic: bool

    @property
    def stem(self) -> str:
        return self.path.stem


def new_batch_id(now: datetime | None = None) -> str:
    """`2026-09-09T12-00-00Z-run01` -- filename-safe, sorts chronologically."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ-run01")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def describe(path: Path) -> DiscoveredFile:
    stat = path.stat()
    with open(path, "rb") as fh:
        magic = fh.read(len(_PDF_MAGIC))
    return DiscoveredFile(
        path=path,
        filename=path.name,
        size_bytes=stat.st_size,
        sha256=_sha256(path),
        modified_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        has_pdf_magic=magic == _PDF_MAGIC,
    )


def discover_files(root: str | Path | None = None) -> Iterator[DiscoveredFile]:
    """Yield every PDF under `root`, sorted by name so batches are reproducible.

    A file whose bytes do not start with `%PDF-` is still yielded, with
    `has_pdf_magic=False`: the orchestrator quarantines it with a clear reason
    rather than silently skipping a file someone dropped in the folder.
    """
    base = Path(root) if root is not None else settings.data_path("raw")
    if not base.exists():
        return
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield describe(path)
