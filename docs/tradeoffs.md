# Trade-offs

Every architectural decision in `invoice_pipe`, the alternatives that were on
the table, and the reason each one lost or won. Where `docs/plan.md` already
carries a full trade-off table for a decision, this document summarizes it and
points there rather than duplicating it; where a decision was made at
implementation time and isn't in the plan at all, it's recorded here in full.

Nothing below is presented as the only correct answer — each entry ends with
what was given up and the condition under which it's worth reopening.

---

## 1. Extraction: Textract-only vs. Bedrock-vision-only vs. hybrid

**The decision that shapes everything downstream.** Full analysis:
`docs/plan.md` §1.

| | Textract-only | Bedrock-vision-only | **Hybrid (chosen)** |
|---|---|---|---|
| Cost / 1-page doc | ~$0.01 | ~$0.006–0.04 | ~$0.013 |
| Cost / 6-page doc | ~$0.06 | ~$0.04–0.24 | ~$0.065 |
| Digit/OCR reliability | High — purpose-built | Weaker — vision LLMs transpose/merge digits in dense tables | High — inherits Textract |
| Corrupted cmap (sample 1) | Unaffected (raster-based) | Unaffected (raster-based) | Unaffected |
| Scrambled order, 4+ totals (sample 2) | Extraction fine; **can't decide which total is canonical** | Can decide; costlier, weaker OCR | Textract extracts, Bedrock decides |
| Vendor vocabulary → fixed ontology | **Cannot do this at all** | Can do this | This is the entire reason for the split |

**Why hybrid won.** Textract is structurally immune to two of the six samples'
worst failure modes (corrupted font mapping, scrambled reading order) because
it OCRs the rendered raster and never touches the broken text stream. But
Textract has no concept of *meaning* — it returns `"Grand Total: 0.00"` and
`"Last bill amount: 1611.64"` as equally valid label/value pairs, with no way
to rank them. A vision LLM could rank them, but at full image-token cost to
redo OCR that a purpose-built API already does more reliably. Splitting the
work — Textract for spatial extraction, Bedrock (text-only) for semantic
judgment — pays for each capability once, at the point where it's actually
the cheaper and more reliable option.

**What was given up.** A single-vendor call (Textract-only) would be simpler
to operate and cheaper by the Bedrock increment (~$0.003–0.005/doc). That
saving was rejected because it would leave every "which total is canonical"
decision unresolved — not degraded, *unresolved* — which defeats the point of
a fixed 13-field canonical schema across vendors with disjoint vocabularies.

**Revisit when:** Bedrock's per-document cost or latency becomes the
pipeline's actual bottleneck at production volume — at which point the vendor
alias cache (decision 3) is the lever to pull before reconsidering the split
itself.

---

## 2. Extraction fallback: always run both APIs vs. conditional fallback

**Decision.** `AnalyzeExpense` runs first, always. `AnalyzeDocument` with
`FEATURE_TYPES=["FORMS","TABLES"]` only runs when the expense parser's result
is empty or its mean confidence falls under
`textract.fallback_confidence_threshold` (`textract_client.py:75`).

**Alternative considered.** Run both APIs unconditionally and merge, which
would recover a partial FORMS/TABLES hit even when `AnalyzeExpense` mostly
succeeded.

**Trade-off.** Unconditional double-calling guarantees no missed structure at
2x the Textract cost on every document, whether or not the fallback ever adds
anything. Conditional fallback pays the second call's ~$0.065/page only on
the documents that actually need it — image-only or oddly-structured ones —
and accepts a narrow risk: a document that Textract's expense parser scores
confidently but gets subtly wrong on a *specific* field never triggers the
fallback, because the trigger is a document-level average, not a per-field
check.

**Why conditional won.** At the observed failure rate (3 of 6 samples are
scans that plausibly need it; 3 don't), doubling Textract cost on every
document to guard against a narrow per-field miss is a worse trade than
accepting that miss and catching it downstream — `LOW_FIELD_CONFIDENCE`
(decision 6) exists precisely to surface a confident-but-wrong field without
needing a second extraction pass to prevent it.

**Revisit when:** field-level (not document-level) confidence data shows the
fallback would have caught something `LOW_FIELD_CONFIDENCE` didn't — at which
point the trigger condition, not the always-vs-conditional structure, is what
needs revising.

---

## 3. Mapping mechanism: static vendor-alias table vs. LLM-only vs. LLM + cache accelerator

Full analysis: `docs/plan.md` §2 "Mapping mechanism."

**Options considered.**
1. A hand-maintained `{vendor: {label: canonical_field}}` lookup table.
2. Bedrock as the mapper on every single document.
3. Bedrock as the primary mapper, with `vendor_alias_cache.py` as an optional
   accelerator once a vendor's mapping is confidently known.

**Why (1) was rejected outright, not just deprioritized.** VFS, Airtel, and
Gym Lounge share essentially no label vocabulary — `"Grand Total"`,
`"Total Amount Payable"`, `"Payable Amount"` are three different vendors'
words for the same canonical field. A static table doesn't reduce this
problem; it just relocates the mapping logic into `if/else` branches that
need a maintainer's manual edit for every new vendor the pipeline ever sees.
That's not a cheaper version of the LLM approach — it's the exact problem the
LLM approach exists to avoid, wearing a lookup table's clothes.

**Why (3) over (2).** Pure LLM-on-every-document is correct but leaves cost on
the table once a vendor repeats — which, for a *daily-batch* pipeline, is the
expected case, not the exception. The cache
(`vendor_alias_cache.py`) is explicitly **not** the source of truth: Bedrock
remains authoritative, and the cache only replays a label→field association
it already confidently learned from Bedrock, deferring back to the model
whenever coverage is thin (`DEFAULT_MIN_COVERAGE = 0.6`) or the vendor is
unseen. This keeps the correctness property of (2) — every mapping traces
back to a Bedrock decision at some point — while adding the cost property of
something closer to (1) for a repeat vendor, without hand-maintaining
anything.

**What was given up.** The cache introduces staleness risk: if a vendor
changes its invoice template, cached label associations silently return wrong
values until coverage drops below threshold on some other field. Nothing in
the current design detects a *silent* template change — the cache doesn't
version its entries against the source document's confidence in real time.
Documented as a known gap, not solved here.

**Revisit when:** cache hit rate in production data justifies adding
template-change detection (e.g., re-validating a cache hit's field values
against Textract confidence periodically, or expiring entries on a schedule).

---

## 4. Bedrock call shape: forced tool use vs. free-form prompting

**Decision.** `toolConfig.toolChoice = {"tool": {"name": "map_invoice_fields"}}`
(`bedrock_client.py:149`) — the model is *required* to call the tool, not
merely offered it.

**Alternative considered.** A free-form prompt asking the model to "return
JSON matching this schema," parsed with `json.loads()` on the response text.

**Trade-off.** Free-form prompting is more portable across model providers
(works with any chat completion endpoint, not just tool-use-capable ones) and
easier to debug by eye — you're reading the model's own words. Forced tool
use is provider-specific and the response is opaque structured data, not
prose you can skim.

**Why forced tool use won.** The alternative introduces a parsing failure
mode that has nothing to do with mapping quality: a model that reasons
correctly but wraps its JSON in ` ```json ` fences, or prepends "Sure, here's
the mapping:", turns a good mapping into a `json.loads()` exception. Forced
tool use makes the response schema-shaped *by construction* —
`_extract_tool_input` (`bedrock_client.py:166`) only has to check that the
named tool was called at all, not defend against prose. This is also why
`test_prose_answer_is_a_mapping_failure` exists: it's testing that the
*forcing* actually forces, not that parsing is robust to failure.

**What was given up.** Portability. Swapping Bedrock for a different provider
means re-deriving that provider's tool-forcing mechanism, not just changing
an endpoint URL.

---

## 5. Money representation: `Decimal` vs. `float`

**Decision.** Every money field on `InvoiceRecord` is `Decimal`, constructed
via string coercion (`_coerce_money`, `models.py:100`), never via `float()`.

**Trade-off.** `float` is what every JSON parser hands you natively, and
arithmetic on it is faster and requires no import. `Decimal` requires an
explicit coercion step (`Decimal(str(x))`, never `Decimal(x)` from a float
literal — the intermediate string matters) and is marginally slower.

**Why `Decimal` won, decisively.** Binary floating point cannot represent
most decimal fractions exactly — `0.1 + 0.2 != 0.3` in IEEE 754 — which is an
acceptable rounding error in most domains and an unacceptable one in money.
This decision turned out to have a second, independent justification
discovered during implementation, not anticipated in the original design:
boto3's DynamoDB serializer raises `TypeError` outright on `float` for Number
attributes. Choosing `Decimal` for correctness reasons in Stage 2 happened to
be the same choice Stage 4's storage layer would have required regardless —
documented as `docs/plan.md`'s "Decimal synergy," and as challenge #12 in
`docs/challenges.md` once a *different* field (Textract's `float` confidence
scores) needed the same fix applied a second time.

**What was given up.** Nothing measurable at this scale. The only real cost is
that every money value entering the system must pass through explicit string
coercion rather than being usable as a bare numeric literal — a discipline
`_coerce_money`'s test parametrization (`test_money_coercion`) exists to
enforce at the boundary rather than trust at every call site.

---

## 6. Domain assertion: `>= 0` vs. `> 0`

**Decision.** `_apply_domain_fx_and_dq` (`models.py:181`) hard-fails on
negative money values only. Zero is a valid, loadable value.

**Alternative considered.** Reject `total_amount == 0` as almost certainly a
missed extraction, since a genuinely free invoice is rare.

**Trade-off.** `> 0` catches more likely-broken extractions automatically —
if `total_amount` mapped to `0` because Bedrock picked the wrong label, this
would quarantine it instead of loading garbage. `>= 0` lets that same failure
mode through as a *loaded* record, relying entirely on the soft
`ZERO_TOTAL_AMOUNT` flag to surface it for human review instead of blocking
it.

**Why `>= 0` won.** The VFS sample (`2324GBRAMD125920.pdf`) has a
legitimately printed `Grand Total: 0.00` — not a missed extraction, an actual
document with a zero balance due, appearing on the same page as a non-zero
`Amount: 1,333.00` for unrelated line items. A `> 0` rule would quarantine a
correctly-extracted, valid document because its true content happens to be
zero. Since telling "genuinely zero" apart from "extraction picked the wrong
field and it defaulted to zero" is not something a domain constraint can do
— it requires the semantic context a human or the DQ flag review has, not a
number comparison — the rule was set to the least destructive threshold
(`>= 0`) and the ambiguity was pushed to a *reviewable* soft flag instead of
an unreviewable silent rejection.

**What was given up.** A real extraction bug that produces `0` where a
positive number belongs now loads successfully with a flag, rather than being
caught at the gate. This is a deliberate shift of the failure mode from
"blocked, requiring a re-run" to "loaded with a warning, requiring a human to
notice the flag" — correct for a design that treats zero warnings as evidence
gathering rather than a promise of correctness.

---

## 7. Validator ordering: coerce → domain check → FX conversion → soft flags

**Decision.** A fixed sequence inside `InvoiceRecord`, not an incidental
implementation detail — `docs/plan.md` calls out the ordering explicitly as a
design decision, and the pipeline diagram (`docs/plan.md` §6) draws it as
distinct nodes.

**Why this order and no other.**
- **Coerce before domain check** — the domain check compares typed
  `Decimal`s (`>= 0`); it cannot run against a raw string.
- **Domain check before FX** — failing fast on a negative value *before*
  spending a division and a rounding operation on it means a bad record never
  reaches FX math at all. Reversing this would mean computing a converted
  value for data about to be discarded — wasted work, and worse, an FX-
  converted number could appear in a partially-constructed error state.
- **FX before soft flags** — `ZERO_TOTAL_AMOUNT` and
  `ZERO_TAX_WITH_NONZERO_RATE` must describe the value that is *actually
  stored*. A sub-penny INR amount (`₹0.50`) rounds to `£0.00` after
  conversion; flagging based on the pre-conversion `₹0.50` would silently
  under-report how many records actually have a zero GBP total.
  `test_dq_flags_describe_post_conversion_values` pins this down directly.

**Alternative considered.** Flag before converting, on the theory that flags
should describe "what the vendor actually printed." Rejected because
downstream consumers query the *stored* (post-conversion) value, and a flag
that doesn't match what's queryable is a flag that misleads whoever reads it.

**What was given up.** Nothing recoverable — this ordering has no real
competitor once "the flag must match what's stored" is accepted as the
requirement. The only trade-off is conceptual: a flag can no longer be
interpreted as "what the vendor's own figure implies," only as "what this
system concluded after its own transform."

---

## 8. Nullish-string handling: uniform rule vs. field-class-specific rules

**Decision.** Nullish literals (`"null"`, `"n/a"`, `""`, etc.) are normalized
to `"N/A"` on *optional* string fields (`vat_tax_label`, `product_name`,
`mode_of_payment`, `vat_tax_percentage`) but hard-reject on *mandatory* ones
(`company_name`, `buyer_name`, `invoice_reference_number`, `currency`,
`original_currency`) — two different validators
(`_normalize_nullish_optional_str` vs. `_reject_nullish_mandatory_str`,
`models.py:155` and `:168`), not one shared rule.

**Alternative considered.** One rule, applied everywhere: normalize
`"null"` → `"N/A"` uniformly, regardless of field.

**Trade-off.** A single uniform rule is simpler — one function, one behavior,
easier to reason about. But applied to mandatory fields, it would let
`company_name: "null"` silently become `company_name: "N/A"` and load as a
record with no identifiable vendor — exactly the case
`MISSING_MANDATORY_FIELD` exists to prevent.

**Why the split won.** The vendor's own bug (`GST NO : null`, sample 4) is
real and needs handling, but *what* it's acceptable to paper over depends on
whether the field can function as `"N/A"` downstream. A tax registration
number defaulting to `"N/A"` is inert. A company name defaulting to `"N/A"`
poisons every downstream vendor-partition key
(`VENDOR#NA`) and silently merges unrelated invoices from different unnamed
vendors into one DynamoDB partition. The split enforces that distinction at
the type level rather than trusting every future field addition to remember
it.

**What was given up.** Two validator functions to maintain instead of one,
and a rule that a future contributor must consciously choose between when
adding a 14th field — there's no single obvious default to copy-paste.

---

## 9. `product_name`: flat semicolon-joined string vs. structured line-item field

**Decision.** `product_name` stays a flat `str` on `InvoiceRecord`
(`"Gym Membership Fee; Registration Fee"`), matching the brief's `str`-typed
schema. The real structured rows — from Textract's `LineItemGroups`/Tables
geometry — are additionally preserved, lossless, as JSON under
`unmapped_metadata["line_items"]` (`bedrock_client.py:_finalize`).

**Alternative considered.** Change `product_name`'s type to a list of
line-item objects, since two real samples (Gym Lounge, Airtel) genuinely have
multi-row tables and a flat string is a lossy representation of that.

**Trade-off.** A structured list is the more honest representation of what's
actually on the document and would let a downstream consumer query individual
line items without re-parsing a semicolon-joined string. It also breaks the
brief's stated 13-field canonical schema, which types this field as `str`.

**Why the flat string won.** The canonical schema is a stated project
constraint, not a suggestion to improve on. Satisfying it while still not
discarding the real per-row data was solved by *not* forcing one field to do
both jobs: `product_name` satisfies the schema, `unmapped_metadata` carries
the full fidelity. Neither representation is asked to be something it isn't.

**What was given up.** A consumer who wants structured line items has to
reach into `unmapped_metadata["line_items"]` — a JSON string inside a
string-valued dict — rather than a typed field. Documented as the accepted
cost of honoring the fixed schema without silently losing data.

---

## 10. Storage: DynamoDB, with a composite key folded through `normalize()`

**Decision.** `PK = "VENDOR#<normalize(company_name)>"`,
`SK = "INVOICE#<normalize(invoice_reference_number)>"`
(`key_normalization.py`), keeping the human-readable original value on the
item unfolded.

**Alternative considered — key the record on the raw printed strings.**
Simpler: no normalization function, no fold logic, `PK` is just
`f"VENDOR#{company_name}"`.

**Trade-off.** Raw keys are more transparent — the key *is* the value, no
indirection. But the Gym Lounge sample prints its own invoice number two
different ways in one document (`Gym Lounge//2022-2023/240` vs.
`Gym Lounge/ / 2022- 2023/ 240`), and a raw key would silently create two
DynamoDB partitions for what is unambiguously one invoice — defeating the
idempotent-write dedup the whole storage design exists to provide.

**Why normalized keys won.** `normalize()` (uppercase, ASCII-fold, strip
non-`[A-Z0-9]`) collapses both printings to the same key while leaving the
original, human-readable string on the item for display and audit. This is
the cheapest possible fix for a real, observed vendor-formatting
inconsistency, and it generalizes: any future vendor with similar
inconsistent spacing is covered by the same function, not a
per-vendor patch.

**What was given up.** Two genuinely distinct invoices that happen to
normalize to the same folded key (unlikely given the character set collapsed,
but not provably impossible for adversarial input) would collide. Not
observed in any of the 6 samples; treated as an accepted residual risk rather
than solved with a more complex key scheme.

---

## 11. Idempotency mechanism: conditional put vs. read-then-write vs. upsert

**Decision.** `put_item` with
`ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)"`
(`dynamo_client.py:194`); a `ConditionalCheckFailedException` is caught and
routed to `DuplicateRecordError`, not treated as a failure.

**Alternatives considered.**
1. **Read-then-write** — `get_item` first, write only if absent.
2. **Upsert** — always overwrite, whatever was there before.

**Trade-off.** Read-then-write is intuitive but has a race window: two
concurrent batch runs (or a retry racing the original) can both read "absent"
before either writes, and both write, silently duplicating a record that
DynamoDB's own conditional-write mechanism would have caught atomically.
Upsert has no race condition and no duplicate-detection logic to write at
all — but it also has no way to distinguish "reprocessing the same file
safely" from "silently overwriting a record with a bad reprocessing result,"
which matters once a batch can be re-run after a partial failure.

**Why conditional put won.** It's atomic (DynamoDB evaluates the condition
and the write together, closing the race window read-then-write leaves open)
and it turns "this file was already loaded" into a *distinguishable,
loggable outcome* (`DUPLICATE_SKIPPED`) rather than either a silent
overwrite or a race-prone check. This is also what makes reprocessing a whole
batch after a partial failure safe by construction: re-running five already-
loaded files and one newly-fixed one produces five `DUPLICATE_SKIPPED` and
one `LOADED`, with no manual bookkeeping about which files were "already
done."

**What was given up.** A record can never be corrected by re-running the same
batch — fixing a bad mapping requires either a new invoice-number-bearing
document or a deliberate delete-then-reload, not a re-run. Accepted because
silent overwrite (upsert) was judged the worse failure mode: an operator
should have to take a deliberate action to replace a loaded record, not have
it happen as a side effect of re-running a batch for an unrelated reason.

---

## 12. Two GSIs (date, status) vs. more, fewer, or a single composite index

**Decision.** Exactly two Global Secondary Indexes:
`GSI1-DateIndex` (`DATE#<yyyy-mm>` / `<date>#<PK>`) and
`GSI2-StatusIndex` (`STATUS#<status>` / `<ingested_at_utc>`)
(`dynamo_client.py:39-40`).

**Alternative considered — a single GSI combining both dimensions**, e.g.
`GSI1PK = "STATUS#<status>#DATE#<yyyy-mm>"`, to halve the write cost (each
GSI write is billed).

**Trade-off.** One combined GSI is cheaper per write and simpler to
provision, but it only serves queries that filter on *both* dimensions
together, or on the leading one. "All invoices in April 2026, any status" and
"everything currently quarantined, any month" are both real, independent
questions the design explicitly names as target queries
(`docs/plan.md` §4) — a combined key would force a full `Query` scan-and-
filter for whichever dimension isn't the key prefix, which is exactly the
`Scan`-avoidance the GSI exists to provide in the first place.

**Why two separate GSIs won.** Each index serves one clean, independent
access pattern with a true `Query`, not a `Query`-then-filter. The extra
write cost is negligible at the item counts here (each `put_item` writes the
base table plus two GSI entries — three write-request-units instead of one,
still fractions of a cent per record at the volumes discussed in
`docs/challenges.md` and the AWS cost breakdown given in conversation) and
becomes a real line item only well past the point where the design's own
migration trigger (below) would already be forcing a bigger rethink anyway.

**What was given up.** A third, cheaper-at-scale query pattern — say,
"quarantined records for a given month" — isn't a single indexed `Query`
under this scheme; it requires querying one GSI and filtering client-side. Not
a named requirement, so not paid for.

---

## 13. Quarantine as filesystem sidecar + DynamoDB tombstone vs. either alone

**Decision.** A hard failure produces *two* things:
`data/quarantine/<file>.error.json` (`sidecar_writer.py`) with the full
Pydantic error list and the pre-Pydantic payload, **and** a minimal tombstone
item in DynamoDB (`PK="QUARANTINE#<batch_id>"`,
`GSI2PK="STATUS#QUARANTINED"`) — even though the quarantined record itself
never reaches the main table.

**Alternative considered — filesystem sidecar only**, since the design
already commits to "quarantined records are filesystem-only by design" for
the record data itself.

**Trade-off.** Filesystem-only is simpler (one write, one system to check)
and keeps quarantine entirely out of the database that's meant to hold
*loaded* invoices. But it means answering "what's our pipeline's current
quarantine rate" requires scanning two systems — the DynamoDB table for
loaded/duplicate status, and the filesystem for quarantine status — which is
exactly the kind of operational friction the GSI2-StatusIndex exists to
eliminate for the other two outcomes.

**Why the tombstone won despite the redundancy.** A few bytes per
quarantined file (`PK`, `SK`, `GSI2PK`, `GSI2SK`, `status`, `error_type`,
`batch_id`) buys back "pipeline status is answerable from storage alone" —
one `Query` against `GSI2-StatusIndex` for `STATUS#QUARANTINED` returns every
quarantined file across every batch, with the full diagnostic detail still
living in the sidecar for whoever needs to actually fix the record.

**What was given up.** Two systems now need to agree — a tombstone written
but its sidecar write failing (or vice versa) is possible, since they're two
separate I/O operations with no shared transaction. `_quarantine()`
(`run_batch.py:267`) writes the sidecar first and treats the tombstone write
as best-effort (wrapped in its own `try/except`, logged but non-fatal) —
explicitly prioritizing "the diagnostic record always exists" over "the two
records are always consistent."

---

## 14. Batch statistics: scan-and-compute vs. Athena-on-export

**Decision.** `src/stats/batch_stats.py` reads via `scan_all()` (or
`query_month()` against GSI1) and aggregates in plain Python, in-process.

**Alternative considered.** Export the table to S3 and query it with Athena
— the standard AWS pattern for DynamoDB analytics at scale, since DynamoDB
has no native `GROUP BY`/`SUM`.

**Trade-off.** Athena is the right answer at scale: it's how you avoid
reading every item into application memory once "every item" is millions of
rows, and it gives you real SQL aggregation instead of a hand-rolled
`Counter`. It is also genuinely more infrastructure — an export pipeline, an
Athena table definition, a query layer, none of which pays for itself at low
item counts, where its main effect would be adding a $/query cost and a
data-freshness lag (export → query, not live) to a problem a five-second scan
already solves for free.

**Why scan-and-compute won, for now.** At the current and realistically
near-term scale — 6 samples growing to low thousands of invoices — a filtered
`Scan` costs a handful of read-request-units and returns sub-second. Building
the Athena pipeline now would be solving a scale problem that doesn't exist
yet, at the cost of infrastructure that does.

**This is the one decision in this document with an explicit, numeric
reopen trigger, not a vague "at scale" gesture**: `batch_stats.py` hardcodes
`MIGRATION_ITEM_THRESHOLD = 1_000_000` and every computed `BatchStats` result
carries `exceeds_migration_threshold` as a real field — so the decision to
migrate isn't a judgment call made from memory later, it's a boolean the
stats job already reports on every run.

---

## 15. Batch resilience: per-file exception boundary vs. fail-fast

**Decision.** `run_batch()` (`run_batch.py:317`) wraps each file's full
extract→map→validate→store run in its own `try/except`; one file's failure
never aborts the loop.

**Alternative considered.** Fail the whole batch on the first hard error,
which is the simpler control flow and arguably the safer default for a
pipeline where a systemic failure (e.g., Bedrock is down entirely) shouldn't
be quietly absorbed as "every file individually failed."

**Trade-off.** Fail-fast catches a systemic outage immediately and loudly —
the first `BedrockMappingError` stops everything, which is exactly the signal
you want if the *cause* is infrastructure rather than any one document.
Per-file isolation risks masking that same systemic failure as "6/6 files
quarantined" instead of "the pipeline itself is broken," if nobody reads the
batch summary closely.

**Why per-file isolation won.** The six real samples establish that
*document-level* failure is the expected case, not the exception — 3 of 6 are
scans, 1 has a corrupted font map, 1 has a negative-total edge case invented
specifically to prove the quarantine path works. A daily batch pipeline that
aborts entirely because file 3 of 40 is malformed would quarantine nothing
and load nothing, which is strictly worse than quarantining the one bad file
and loading the other 39. The systemic-failure risk is mitigated a different
way: `BatchResult.counts()` and `flag_counts()` surface aggregate outcome
counts every run, so "40/40 quarantined" is visible in the summary even
though it didn't halt execution — it's a monitoring problem, not a control-
flow one.

**What was given up.** A genuine infrastructure outage produces N individual
`EXTRACTION_FAILED` sidecars instead of one clear "Bedrock is down" error at
the top of the run. Nothing in the current design distinguishes "40 documents
each independently failed for document reasons" from "40 documents failed
because of one shared cause" — both look identical in the sidecar count.
Recognizing that distinction, if it becomes a real operational problem,
would mean adding failure-clustering to the batch summary, not changing the
per-file isolation itself.

---

## 16. Testability: real offline replay clients vs. AWS-required testing only

**Decision.** `ReplayTextractClient` / `ReplayBedrockMapper`
(`textract_client.py:191`, `bedrock_client.py:241`) implement the exact same
interface as their real-AWS counterparts and are swapped in by
`Dependencies.build(offline=True)` — not a test-only mock, but a genuine
runtime mode reachable via `--offline` on the CLI.

**Alternative considered.** Require AWS credentials to run the pipeline at
all; test purely against `moto` and hand-rolled fakes, with no non-AWS way to
run the *full* batch end to end.

**Trade-off.** AWS-required-always is simpler — one code path, no
`Dependencies` indirection, no silver-replay logic to maintain as a second
consumer of the persisted JSON. But it means nobody can run, demo, or debug
the full pipeline — including the quarantine and duplicate-skip paths —
without provisioning a Textract/Bedrock/DynamoDB-capable AWS account first,
and every iteration on the Bedrock mapping prompt during development would
cost real money and real latency per attempt.

**Why the replay mode won.** The persisted-silver-output requirement
(`data/silver/*.textract.json` / `*.mapped.json`) already existed in the
design for replay/debugging without re-billing AWS
(`docs/plan.md` §1, call sequence step 4). Building `ReplayTextractClient` /
`ReplayBedrockMapper` against that same file shape wasn't new infrastructure —
it was making an existing requirement runnable, at the cost of one `if
offline` branch in `Dependencies.build()`. The entire integration suite
(`tests/integration/test_pipeline.py`) runs against this mode, meaning the
orchestration logic — batch resilience, quarantine sidecars, duplicate
detection, gold mirroring — is exercised as real code paths, not asserted
against a mock of the orchestrator itself.

**What was given up.** The replay clients are a second implementation of
"what does `analyze()` / `map_document()` return," which must be kept in
sync with the real clients' interface by discipline, not by the type system
alone (both `TextractClient.analyze()` and `ReplayTextractClient.analyze()`
share a signature but nothing enforces it beyond convention). Documented as
challenge #16 in `docs/challenges.md`.

**Reversed.** Once the pipeline had been proven end to end against real
AWS — one document fully through Textract, Bedrock, and a real DynamoDB
write, verified by reading it back through both GSIs — the project owner
asked for `--offline` removed from the production code entirely: the
pipeline should behave identically whether it's processing 1 file or 500,
against real infrastructure every time, with no second code path to keep in
sync. `ReplayTextractClient`, `ReplayBedrockMapper`, `InMemoryStore`, the
`--offline`/`--aws` CLI flags, and `scripts/seed_silver.py` were all deleted
from `src/`. The exact cost named above — a second implementation drifting
from the real one by convention, not by the type system — is what made this
an easy call once the alternative (moto + fixture-routed test doubles,
living only in `tests/`, covered in decision 16b below) could serve the same
testing need without being reachable from production at all.

---

## 16b. Removing offline mode: what replaced it in the test suite

**Decision.** Decision 16 was reversed (see above), but the tests that
depended on `ReplayTextractClient` / `ReplayBedrockMapper` / `InMemoryStore`
(`tests/integration/test_pipeline.py`, `test_s3_batch.py` — roughly 30 tests
covering per-file isolation, quarantine, dedup, gold mirroring, and stats)
couldn't simply lose their fixtures; batch-orchestration coverage still had
to exist. They were rewritten against two different real testing
mechanisms, matched to what each leg actually needed:

- **Storage**: `InMemoryStore` → real `DynamoStore` against a moto-mocked
  table. Moto fully supports DynamoDB, so this is strictly more faithful —
  conditional writes, GSI queries, and table creation all now exercise the
  real `DynamoStore` code, not a simplified dict-based stand-in.
- **Extraction/mapping**: no moto equivalent exists for Textract or Bedrock,
  so `FixtureTextractClient` / `FixtureBedrockMapper` (`tests/conftest.py`)
  were introduced — test doubles matching `TextractClient.analyze()` /
  `BedrockMapper.map_document()`'s public interface, routing by filename to
  the real fixture JSON in `tests/fixtures/`, and calling the *real* parsers
  (`from_expense_response`, `from_document_analysis_response`) rather than
  replaying pre-parsed output. AWS call mechanics — polling, pagination, the
  FORMS+TABLES fallback trigger, forced tool use — stay covered directly
  against `FakeTextract` / `FakeBedrock` in `test_textract_mock.py` /
  `test_bedrock_mock.py`; these new doubles exist only to let orchestration
  tests process many distinct documents in one batch without re-proving
  mechanics those other files already cover.

**Why this isn't just decision 16 under a new name.** The critical
difference is reachability. `ReplayTextractClient` lived in `src/`, was
importable by production code, and was one CLI flag away from actually
running — it was a real, if optional, way to operate the pipeline without
AWS. `FixtureTextractClient` lives in `tests/conftest.py`; nothing in `src/`
or the CLI can import it. The production pipeline has exactly one path
after this change. The test suite still needs fast, realistic doubles —
that's not offline mode, that's just how AWS-integrated code gets tested
without a bill on every `pytest` run, the same reasoning `moto` itself is
built on.

**What was given up, again.** The same synchronization risk decision 16
named — `FixtureTextractClient.analyze()` and `TextractClient.analyze()`
share a signature by convention, not by an enforced contract — still
exists, just relocated to test-only code where a drift is a test-suite
problem to fix, not a production behavior a user could silently be running
against.

---

## 17. Extraction targeting: Textract Queries vs. generic extraction + Bedrock mapping

**Decision.** Textract is never told what to look for. `start_expense_analysis`
(`textract_client.py:136`) takes only a document location; the FORMS+TABLES
fallback's `FeatureTypes=["FORMS","TABLES"]` selects detection *modes*, not
field names. Every canonical-field decision happens downstream, in Bedrock --
Textract's own output has no idea the 13-field schema exists.

**Alternative considered.** AWS Textract offers a `QUERIES` feature type:
pass literal natural-language questions ("What is the total amount?", "Who
is the vendor?") and Textract returns direct answers in the same call, with
no second API and no separate LLM in the loop at all.

**Trade-off.** Queries would collapse extraction and mapping into one
Textract call -- one API, one cost line, likely lower latency, and schema
changes would be a config-only edit either way (a query list instead of a
prompt). But a Query returns one answer with no visible reasoning: it
exposes no mechanism for *why* it picked one figure over another when
several plausible candidates exist on the page, and nothing analogous to
this project's `multiple_total_candidates` flag exists to mark that a
judgment call was made at all.

**Why generic-extraction-plus-Bedrock won.** Not decided through a formal
trade-off analysis in `docs/plan.md` -- this alternative was never evaluated
there. It's a judgment call made after the fact, once the real Textract
output for the Zomato-receipt sample (`Order_ID_7104598035.pdf`, called for
real against live AWS, not a fixture) showed exactly the shape of ambiguity
Queries would have hidden: a `Total: ₹235.93` built from five separately
labeled charges -- delivery subtotal, surge fee, packaging charge, platform
fee, tax -- minus a coupon, with nothing on the page marking that specific
combination as *the* total. A Query asking "what is the total" would very
likely answer correctly here (Textract's own `TOTAL` field type already
matched the right line), but silently -- with no record that four other
candidate charges were in play, and no flag distinguishing this from a
document where the arithmetic doesn't cleanly resolve to one obvious line.

**What was given up.** Architectural simplicity, and one fewer AWS service
call. Queries are billed per query per page on top of `AnalyzeExpense`'s own
cost, so the two-API version isn't obviously more expensive either -- the
real cost of this decision is operational (two services to monitor, two
failure modes to handle) rather than financial. Also given up: adding a
canonical field would need its *question* added to a `QueriesConfig` in the
extraction call under a Queries design, coupling schema changes to the
extraction stage rather than isolating them entirely to
`config/canonical_schema.yaml`, as they are now.

**Revisit when:** Bedrock cost or latency becomes the pipeline's actual
bottleneck at volume, and audit-trail visibility into *why* a value was
chosen turns out not to be a real requirement in practice.

---

## Cross-reference

| Decision | Plan.md section | Challenge (if any) |
|---|---|---|
| §1 Extraction split | `docs/plan.md` §1 | — |
| §3 Mapping mechanism | `docs/plan.md` §2 "Mapping mechanism" | #6, #13, #14 |
| §5 Decimal vs float | `docs/plan.md` §2 "Decimal synergy" | #12 |
| §6 `>= 0` domain check | `docs/plan.md` §2 "Design decisions" | #5 |
| §7 Validator ordering | `docs/plan.md` §2 + §6 diagram note | — |
| §9 `product_name` flat | `docs/plan.md` §2 "Product Name(s) tension" | — |
| §10 Key normalization | `docs/plan.md` §4 "Table & key design" | #9 |
| §11 Conditional put | `docs/plan.md` §4 "Idempotent write" | — |
| §12 Two GSIs | `docs/plan.md` §4 "Global Secondary Indexes" | — |
| §13 Quarantine tombstone | `docs/plan.md` §4 GSI2 note | — |
| §14 Scan-and-compute stats | `docs/plan.md` §4 "Batch statistics" | #17 |
| §15 Per-file isolation | `docs/plan.md` §3 "Batch resilience" | — |
| §16 Offline replay | not in plan.md — implementation-time decision, later reversed | #16, #19 |
| §16b Test doubles after removal | not in plan.md — implementation-time decision | — |
| §17 Textract Queries vs. Bedrock mapping | not in plan.md — decided after a real AWS call | — |
