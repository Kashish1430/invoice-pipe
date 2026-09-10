"""Typed shapes for Textract's raw output.

Textract returns two quite different envelopes -- `AnalyzeExpense`'s
SummaryFields/LineItemGroups and `AnalyzeDocument`'s flat Block graph -- and the
mapping layer should not care which one produced a given document. Both are
normalised here into one `ExtractedDocument`, which is also exactly what gets
persisted to `data/silver/<file>.textract.json` for replay.

Nothing here maps to canonical fields. Labels stay verbatim, in the vendor's own
vocabulary; deciding what they *mean* is Stage 2's job.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

_LABEL_WS = re.compile(r"\s+")
_LABEL_LEADING_DECORATION = re.compile(r"^[\-\*•\s]+")


def normalize_label(label: str) -> str:
    """Collapse whitespace and strip leading bullet/dash decoration.

    Textract sometimes OCRs a stray bullet or dash onto its own line ahead of
    a label -- "-\\nCoupon (TASTENEW)" rather than "Coupon (TASTENEW)". A
    mapper citing that label back as a source reasonably reports the cleaned
    version. Confidence lookup has to compare both sides on the same
    normalized basis, or a real match is silently missed even though the
    label and its confidence both genuinely exist (see docs/challenges.md).
    """
    collapsed = _LABEL_WS.sub(" ", label).strip()
    return _LABEL_LEADING_DECORATION.sub("", collapsed).strip()


@dataclass
class SummaryField:
    """One label/value pair from the document header or footer.

    `type_` is Textract's normalised enum (TOTAL, VENDOR_NAME,
    INVOICE_RECEIPT_DATE, ...) and is `OTHER` when Textract could not classify
    the pair; `label` is what the vendor actually printed.
    """

    type_: str
    label: str
    value: str
    confidence: float = 0.0
    label_confidence: float = 0.0
    page: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LineItem:
    """One row of a line-item table, as label -> value in the vendor's terms."""

    fields: dict[str, str] = field(default_factory=dict)
    # Per-column confidence, keyed the same as `fields`. `confidence` above
    # stays as the row-level average (kept for the fallback-trigger and
    # general row-quality use it already had); this is what lets a single
    # canonical field sourced from one column get its own real score instead
    # of inheriting the whole row's average.
    field_confidences: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    row_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def description(self) -> str:
        """Best-effort row description, for the semicolon-joined product_name."""
        for key in ("ITEM", "DESCRIPTION", "PRODUCT_CODE", "NAME"):
            for k, v in self.fields.items():
                if k.upper() == key and v.strip():
                    return v.strip()
        # No recognised description column: fall back to the longest non-numeric
        # cell, which in practice is the description.
        candidates = [
            v.strip()
            for v in self.fields.values()
            if v.strip() and not v.strip().replace(",", "").replace(".", "").isdigit()
        ]
        return max(candidates, key=len) if candidates else ""


@dataclass
class ExtractedDocument:
    """Everything Textract produced for one PDF, API-agnostic."""

    source_file: str
    api: str = "AnalyzeExpense"  # or "AnalyzeDocument" when the fallback ran
    job_id: str | None = None
    page_count: int = 1
    summary_fields: list[SummaryField] = field(default_factory=list)
    line_items: list[LineItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- derived views ------------------------------------------------------
    def label_value_pairs(self) -> list[dict[str, Any]]:
        """Compact form handed to Bedrock -- small enough to keep the call cheap."""
        return [
            {
                "type": f.type_,
                "label": f.label,
                "value": f.value,
                "confidence": round(f.confidence, 2),
                "page": f.page,
            }
            for f in self.summary_fields
        ]

    def confidence_by_label(self) -> dict[str, float]:
        """Normalized label -> Textract confidence, keyed as Bedrock will cite it.

        When a label repeats across pages (multi-page statements), the highest
        confidence wins: the mapper picked one occurrence, and penalising it for
        a weaker duplicate elsewhere would flag documents that are actually fine.
        """
        out: dict[str, float] = {}
        for f in self.summary_fields:
            key = normalize_label(f.label)
            if key:
                out[key] = max(out.get(key, 0.0), round(f.confidence, 2))
        return out

    def line_item_confidence_by_label(self) -> dict[str, float]:
        """Normalized column label -> worst-case confidence across every row.

        A canonical field like `product_name` can be built by joining several
        line-item rows into one semicolon-separated string (see
        docs/tradeoffs.md #9); the minimum across rows is the honest
        confidence for that joined value -- an average would hide a
        genuinely weak row behind stronger ones, which is exactly the
        failure mode LOW_FIELD_CONFIDENCE exists to catch. For the common
        case of a single-row document this is just that row's own score.
        """
        out: dict[str, float] = {}
        for li in self.line_items:
            for label, conf in li.field_confidences.items():
                key = normalize_label(label)
                if key:
                    out[key] = min(out.get(key, 100.0), round(conf, 2))
        return out

    def mean_confidence(self) -> float:
        vals = [f.confidence for f in self.summary_fields if f.confidence]
        return statistics.fmean(vals) if vals else 0.0

    def is_usable(self, threshold: float) -> bool:
        """Whether the expense parser produced anything worth mapping.

        Drives the AnalyzeDocument FORMS+TABLES fallback: an empty summary set,
        or one whose average confidence sits under the floor, means the
        expense-specific parser missed the document's structure.
        """
        if not self.summary_fields:
            return False
        return self.mean_confidence() >= threshold

    # ---- persistence --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "api": self.api,
            "job_id": self.job_id,
            "page_count": self.page_count,
            "summary_fields": [f.to_dict() for f in self.summary_fields],
            "line_items": [li.to_dict() for li in self.line_items],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExtractedDocument":
        return cls(
            source_file=payload["source_file"],
            api=payload.get("api", "AnalyzeExpense"),
            job_id=payload.get("job_id"),
            page_count=payload.get("page_count", 1),
            summary_fields=[SummaryField(**f) for f in payload.get("summary_fields", [])],
            line_items=[LineItem(**li) for li in payload.get("line_items", [])],
            warnings=list(payload.get("warnings", [])),
        )


# --------------------------------------------------------------------------
# Parsers for the two raw AWS envelopes
# --------------------------------------------------------------------------
def _expense_text(node: dict[str, Any] | None) -> tuple[str, float]:
    if not node:
        return "", 0.0
    return (node.get("Text") or "").strip(), float(node.get("Confidence") or 0.0)


def from_expense_response(
    response: dict[str, Any], source_file: str, job_id: str | None = None
) -> ExtractedDocument:
    """Normalise a `GetExpenseAnalysis` / `AnalyzeExpense` response."""
    doc = ExtractedDocument(source_file=source_file, api="AnalyzeExpense", job_id=job_id)
    documents = response.get("ExpenseDocuments") or []
    doc.page_count = max(len(documents), 1)

    for expense_doc in documents:
        page = int(expense_doc.get("ExpenseIndex") or 1)
        for sf in expense_doc.get("SummaryFields") or []:
            label, label_conf = _expense_text(sf.get("LabelDetection"))
            value, value_conf = _expense_text(sf.get("ValueDetection"))
            type_ = (sf.get("Type") or {}).get("Text") or "OTHER"
            if not label:
                # Textract classified the pair but the printed label was
                # implicit (a bare figure in a totals block); the enum is the
                # only handle the mapper has on it.
                label = type_
            doc.summary_fields.append(
                SummaryField(
                    type_=type_,
                    label=label,
                    value=value,
                    confidence=value_conf,
                    label_confidence=label_conf,
                    page=page,
                )
            )

        row_index = 0
        for group in expense_doc.get("LineItemGroups") or []:
            for item in group.get("LineItems") or []:
                cells: dict[str, str] = {}
                cell_confidences: dict[str, float] = {}
                confidences: list[float] = []
                for ef in item.get("LineItemExpenseFields") or []:
                    key = (ef.get("Type") or {}).get("Text") or ""
                    label, _ = _expense_text(ef.get("LabelDetection"))
                    value, conf = _expense_text(ef.get("ValueDetection"))
                    cell_key = label or key or f"COL{len(cells)}"
                    cells[cell_key] = value
                    if conf:
                        confidences.append(conf)
                        cell_confidences[cell_key] = conf
                if cells:
                    doc.line_items.append(
                        LineItem(
                            fields=cells,
                            field_confidences=cell_confidences,
                            confidence=statistics.fmean(confidences) if confidences else 0.0,
                            row_index=row_index,
                        )
                    )
                    row_index += 1
    return doc


def from_document_analysis_response(
    response: dict[str, Any], source_file: str, job_id: str | None = None
) -> ExtractedDocument:
    """Normalise an `AnalyzeDocument`/`GetDocumentAnalysis` FORMS+TABLES response.

    This envelope is a flat Block graph: KEY_VALUE_SET blocks point at their
    value block and at the WORD blocks holding the text, so recovering a
    label/value pair means walking Relationships rather than reading a field.
    """
    blocks = response.get("Blocks") or []
    by_id = {b["Id"]: b for b in blocks if "Id" in b}

    def text_of(block: dict[str, Any]) -> str:
        parts: list[str] = []
        for rel in block.get("Relationships") or []:
            if rel.get("Type") != "CHILD":
                continue
            for cid in rel.get("Ids") or []:
                child = by_id.get(cid, {})
                if child.get("BlockType") == "WORD":
                    parts.append(child.get("Text", ""))
                elif child.get("BlockType") == "SELECTION_ELEMENT":
                    parts.append(
                        "SELECTED"
                        if child.get("SelectionStatus") == "SELECTED"
                        else "NOT_SELECTED"
                    )
        return " ".join(p for p in parts if p).strip()

    doc = ExtractedDocument(
        source_file=source_file, api="AnalyzeDocument", job_id=job_id
    )
    doc.page_count = max(
        [int(b.get("Page") or 1) for b in blocks] or [1]
    )

    for block in blocks:
        if block.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in (block.get("EntityTypes") or []):
            continue
        label = text_of(block)
        value, value_conf = "", 0.0
        for rel in block.get("Relationships") or []:
            if rel.get("Type") != "VALUE":
                continue
            for vid in rel.get("Ids") or []:
                value_block = by_id.get(vid)
                if value_block:
                    value = text_of(value_block)
                    value_conf = float(value_block.get("Confidence") or 0.0)
        if label or value:
            doc.summary_fields.append(
                SummaryField(
                    type_="OTHER",  # the FORMS parser has no normalised enums
                    label=label or "UNLABELLED",
                    value=value,
                    confidence=value_conf,
                    label_confidence=float(block.get("Confidence") or 0.0),
                    page=int(block.get("Page") or 1),
                )
            )

    doc.line_items.extend(_table_rows(blocks, by_id, text_of))
    doc.warnings.append(
        "recovered via AnalyzeDocument FORMS+TABLES fallback; labels are "
        "unnormalised (no Textract expense enums available)"
    )
    return doc


def _table_rows(
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    text_of: Any,
) -> Iterable[LineItem]:
    """Rebuild TABLE blocks into rows, treating row 1 as the header."""
    for table in blocks:
        if table.get("BlockType") != "TABLE":
            continue
        cells: list[dict[str, Any]] = []
        for rel in table.get("Relationships") or []:
            if rel.get("Type") != "CHILD":
                continue
            for cid in rel.get("Ids") or []:
                cell = by_id.get(cid, {})
                if cell.get("BlockType") == "CELL":
                    cells.append(cell)
        if not cells:
            continue

        grid: dict[int, dict[int, str]] = {}
        cell_conf: dict[int, dict[int, float]] = {}
        for cell in cells:
            r, c = int(cell.get("RowIndex", 0)), int(cell.get("ColumnIndex", 0))
            grid.setdefault(r, {})[c] = text_of(cell)
            cell_conf.setdefault(r, {})[c] = float(cell.get("Confidence") or 0.0)

        if len(grid) < 2:
            continue
        header_row = min(grid)
        headers = {
            c: (v.strip() or f"COL{c}") for c, v in grid[header_row].items()
        }
        for idx, r in enumerate(sorted(k for k in grid if k != header_row)):
            row = {
                headers.get(c, f"COL{c}"): v for c, v in grid[r].items() if v.strip()
            }
            row_conf = {
                headers.get(c, f"COL{c}"): conf
                for c, conf in cell_conf.get(r, {}).items()
                if grid[r].get(c, "").strip()
            }
            if row:
                confs = list(row_conf.values())
                yield LineItem(
                    fields=row,
                    field_confidences=row_conf,
                    confidence=statistics.fmean(confs) if confs else 0.0,
                    row_index=idx,
                )
