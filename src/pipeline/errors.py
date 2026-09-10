"""Pipeline exception hierarchy.

`run_batch` catches these by type to decide quarantine-vs-skip, so each stage
raises its own class rather than a bare `Exception`. Not called out as a file in
the Stage-5 layout, but the orchestrator snippet in docs/plan.md names these
types explicitly and they need somewhere to live that neither extraction nor
mapping has to import from each other.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base for anything the per-file exception boundary is expected to catch."""

    stage = "unknown"
    dq_rule = "EXTRACTION_FAILED"


class TextractExtractionError(PipelineError):
    """Textract job failed, timed out, or returned nothing usable."""

    stage = "extraction"


class BedrockMappingError(PipelineError):
    """Bedrock errored, or returned tool input that did not match the schema."""

    stage = "mapping"


class StorageError(PipelineError):
    """DynamoDB write failed for a reason that is not a duplicate key."""

    stage = "storage"
    dq_rule = "EXTRACTION_FAILED"


class DuplicateRecordError(PipelineError):
    """The natural key already exists.

    Not a failure: the record is valid and was loaded by an earlier run. Routed
    to the DUPLICATE_SKIPPED outcome, never to quarantine -- this is what makes
    re-running a batch safe.
    """

    stage = "storage"
    dq_rule = "DUPLICATE_SKIPPED"
