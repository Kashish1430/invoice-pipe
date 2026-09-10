"""Optional cost optimisation: reuse a vendor's already-learned label mapping.

Explicitly *not* the primary mapping mechanism. A static vendor-alias table was
rejected for that role in docs/plan.md: VFS, Airtel and Gym Lounge share no
label vocabulary, so a hand-maintained table degenerates into vendor-specific
if/else and needs an edit for every new vendor.

What this is instead: once Bedrock has confidently mapped a vendor's label set,
the label -> canonical-field association is remembered, and later invoices from
the same vendor skip the model call. Bedrock stays the source of truth. Every
uncertain case -- unknown vendor, thin coverage, a required field the cached
labels cannot fill -- returns `None` and falls straight back to the model.

`apply()` returns a plain dict rather than a `MappedPayload` so this module
stays importable by `bedrock_client` without a cycle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import settings
from src.extraction.raw_shapes import ExtractedDocument
from src.mapping import field_semantics
from src.storage.key_normalization import normalize

#: Textract enums that identify the seller, best first.
_VENDOR_TYPES = ("VENDOR_NAME", "SUPPLIER_NAME", "RECEIVER_NAME")
_VENDOR_LABEL_HINTS = ("vendor", "company", "supplier", "merchant", "seller", "from")

DEFAULT_MIN_COVERAGE = 0.6

#: Canonical fields whose *value* may be reused across a vendor's invoices,
#: not just their label association. Deliberately tiny: currency is a property
#: of the vendor's billing, and is usually inferred from a symbol rather than
#: read off a label, so without this the cache could never satisfy the required
#: -field check. Nothing that varies invoice to invoice belongs here.
_CONSTANT_FIELDS = ("currency",)


class VendorAliasCache:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        min_coverage: float = DEFAULT_MIN_COVERAGE,
    ):
        self.path = (
            Path(path)
            if path is not None
            else settings.data_path("silver") / "_vendor_alias_cache.json"
        )
        self.min_coverage = min_coverage
        self._entries: dict[str, dict[str, Any]] = self._read()

    # ---- persistence --------------------------------------------------------
    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            # A corrupt accelerator must never break the pipeline: an unreadable
            # cache is simply an empty one, and every document goes to Bedrock.
            return {}

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, indent=2, ensure_ascii=False, sort_keys=True)

    # ---- vendor identification ---------------------------------------------
    @staticmethod
    def vendor_key(doc: ExtractedDocument) -> str | None:
        """Identify the seller from raw Textract output, before any mapping.

        Chicken-and-egg: `company_name` is a mapping output, but the cache has
        to be keyed before the mapping runs. Textract's own VENDOR_NAME enum is
        used where present, with a label-text heuristic behind it; when neither
        finds a vendor the answer is `None`, which simply means "ask Bedrock".
        """
        for wanted in _VENDOR_TYPES:
            for f in doc.summary_fields:
                if f.type_.upper() == wanted and f.value.strip():
                    return normalize(f.value)
        for f in doc.summary_fields:
            label = f.label.lower()
            if any(h in label for h in _VENDOR_LABEL_HINTS) and f.value.strip():
                return normalize(f.value)
        return None

    # ---- read path ----------------------------------------------------------
    def apply(self, doc: ExtractedDocument) -> dict[str, Any] | None:
        """Rebuild a mapping from cached label associations, or `None` to defer."""
        key = self.vendor_key(doc)
        if not key:
            return None
        entry = self._entries.get(key)
        if not entry or float(entry.get("coverage", 0.0)) < self.min_coverage:
            return None

        label_to_fields: dict[str, list[str]] = entry.get("label_to_fields") or {}
        by_label = {f.label.strip(): f.value.strip() for f in doc.summary_fields}

        defaults = field_semantics.defaults()
        fields_out: dict[str, str] = dict(entry.get("constants") or {})
        source_labels: dict[str, str] = {}
        for label, canonicals in label_to_fields.items():
            value = by_label.get(label, "")
            if not value:
                continue
            for canonical in canonicals:
                fields_out[canonical] = value
                source_labels[canonical] = label

        for name in field_semantics.canonical_field_names():
            fields_out.setdefault(name, defaults.get(name, ""))

        # A cached mapping that cannot fill every required field is not a hit.
        # Falling back costs one model call; guessing costs a wrong record.
        if any(
            not fields_out.get(n, "").strip()
            for n in field_semantics.required_field_names()
        ):
            return None

        return {
            "fields": fields_out,
            "source_labels": source_labels,
            "unmapped_metadata": {},
            "multiple_total_candidates": bool(entry.get("multiple_total_candidates")),
            "notes": f"mapped from vendor_alias_cache entry {key}",
            "model_id": entry.get("model_id", ""),
            "source": "vendor_alias_cache",
            "attempts": 0,
        }

    # ---- write path ---------------------------------------------------------
    def store(self, payload: Any) -> None:
        """Remember a Bedrock mapping. Duck-typed to avoid importing the payload."""
        source_labels: dict[str, str] = dict(getattr(payload, "source_labels", {}) or {})
        company = str((getattr(payload, "fields", {}) or {}).get("company_name", "")).strip()
        if not company or not source_labels:
            return

        names = field_semantics.canonical_field_names()
        fields = getattr(payload, "fields", {}) or {}
        constants = {
            f: str(fields[f]).strip()
            for f in _CONSTANT_FIELDS
            if str(fields.get(f, "")).strip() and f not in source_labels
        }
        covered = {c for c in source_labels if c in names} | set(constants)
        coverage = len(covered) / len(names)
        key = normalize(company)
        previous = self._entries.get(key, {})
        if float(previous.get("coverage", 0.0)) > coverage:
            # Keep the richer mapping: a sparse invoice from a known vendor
            # should not erode what a fuller one already taught the cache.
            return

        # One label can feed several canonical fields -- "SGST(9%):" carries the
        # tax name, its rate and its amount -- so the inverted map holds a list.
        label_to_fields: dict[str, list[str]] = {}
        for canonical, label in source_labels.items():
            label = str(label).strip()
            if canonical in names and label:
                label_to_fields.setdefault(label, []).append(canonical)

        self._entries[key] = {
            "company_name": company,
            "label_to_fields": label_to_fields,
            "constants": constants,
            "coverage": round(coverage, 3),
            "model_id": getattr(payload, "model_id", ""),
            "multiple_total_candidates": bool(
                getattr(payload, "multiple_total_candidates", False)
            ),
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.flush()
