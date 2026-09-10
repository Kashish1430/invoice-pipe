# Challenges & Solutions

Concrete problems hit while building `invoice_pipe`, and what was actually done
about each one. Every item here traces to a real file in this repo — either a
sample PDF in `data/raw/`, a bug that showed up under `pytest`, or a decision
in `docs/plan.md` that turned out to be wrong once tested against real code.
Nothing here is a hypothetical edge case.

---

## 1. The brief didn't match the data

**Challenge.** The project brief referenced "10 sample PDF invoices" and "10
specific target fields." `data/raw/` contains 6 files, and the supplied field
list enumerates 13 fields plus a 14th (`Address`) explicitly called out as
unmapped. Building against the brief's numbers rather than the repo's actual
contents would have produced a schema that didn't fit what was there to
validate it against.

**Solution.** Inspected `data/raw/` directly before designing anything, and
documented both discrepancies up front in `docs/plan.md` §0 rather than
silently reconciling them. The design targets 13 canonical fields + 1
unmapped field, validated against the 6 real files, with an explicit note
that it needs re-validation once the remaining 4 land — because vendor
variance is the entire point of this project, and 4 unseen samples could
surface failure modes the current 6 don't.

---

## 2. Half the samples have no text layer at all

**Challenge.** A first pass at the 6 PDFs (a stdlib-only text-stream parser,
since no PDF libraries were installed yet) showed 3 of 6 files are
image-only scans with zero embedded text. Of the remaining 3, one has a
corrupted font mapping and one has a scrambled text-stream reading order.
That leaves **zero** samples where the PDF's own text layer is reliable
ground truth — a "parse the text layer, OCR only if it's missing" design
would fail on the majority of real inputs from day one.

**Solution.** Textract's `AnalyzeExpense` OCRs the *rendered page raster*,
never the embedded text stream, so it runs uniformly on every document —
scanned or not — with no text-layer branch to get wrong. See
`docs/plan.md` §1 for the full trade-off analysis against a vision-LLM-only
approach.

---

## 3. Corrupted font mapping garbles the buyer name (VFS sample)

**Challenge.** `2324GBRAMD125920.pdf`'s applicant name decodes to garbled
bytes (`' ( 9 . ,  + , 7 ( 6 +  1 $  7 +`) because of a custom glyph mapping
in the PDF. A naive pipeline either crashes trying to make sense of it, or —
worse — an LLM asked to "extract the buyer name" quietly invents a
plausible-looking one instead of reporting what's actually there.

**Solution.** Two layers of defense:
- The Bedrock system prompt (`field_semantics.py:system_prompt`) explicitly
  instructs: garbled text from a corrupted font mapping is passed through
  as-is, never guessed at.
- `InvoiceRecord._reject_nullish_mandatory_str` (`models.py:168`) only
  rejects *nullish* values (`"null"`, `""`, etc.) on mandatory string
  fields — unreadable-but-present text is a valid value, not a validation
  failure. `test_garbled_buyer_name_is_preserved_not_rejected`
  (`tests/unit/test_models.py`) pins this down.

---

## 4. Kerning-split digits corrupt money values (VFS sample)

**Challenge.** The same VFS PDF renders `1,333.00` as `1 1 1,333.00` in its
text stream — the PDF kerns individual glyphs, and the whitespace between
digits is a rendering artifact, not a thousands separator. A regex that
strips commas but not internal whitespace would coerce this to
`Decimal("111333.00")` or fail outright.

**Solution.** `_coerce_money` (`models.py:100`) runs a dedicated pass
before the comma strip:

```python
s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)   # "1 1 1,333.00" -> "111,333.00"
```

parametrized in `test_money_coercion` against the literal string as it
appears in the source file.

---

## 5. The same document contradicts itself (`Grand Total: 0.00` vs `Amount: 1,333.00`)

**Challenge.** The VFS invoice prints `Grand Total: 0.00` in one block and
`Amount: 1,333.00` in another, on the same page. A domain rule of
`total_amount > 0` would reject this file outright — but `0.00` is the
figure carrying the canonical `Grand Total` label, and rejecting the file
loses a real, if internally inconsistent, invoice.

**Solution.** The domain assertion in `models.py:_apply_domain_fx_and_dq`
is `>= 0`, not `> 0` — it hard-fails only on a negative value. A `0.00`
total sets a soft `ZERO_TOTAL_AMOUNT` flag (`dq_rules.py`) but still loads.
The contradiction itself is recorded in the mapper's `notes` field rather
than silently resolved by picking the larger number — see the `notes` in
`tests/fixtures/mapped/2324GBRAMD125920.mapped.json`.

---

## 6. One invoice, six competing "total" figures (Airtel sample)

**Challenge.** The 6-page Airtel statement prints last-bill-amount,
payment-made, this-month's-charges, amount-payable, and
amount-after-due-date as five separate money figures, plus a sixth
"Grand Total" on a later page — all legitimately labeled, none of them
wrong, only one of them the canonical `total_amount`. Deterministic
extraction (Textract's spatial KV pairing) has no way to rank these; it can
find every figure but can't decide which one a downstream query should sum.

**Solution.** This is the reason a language model is in the pipeline at
all, not an incidental feature of it. Bedrock is called with a *forced*
tool use (`toolChoice: {"tool": "map_invoice_fields"}`) over Textract's
label/value JSON, instructed to prefer `Amount Payable` / `Grand Total` /
`Total Due` over balance-forward or surcharge figures
(`field_semantics.py`), and to set `multiple_total_candidates: true` when
it had to choose. That flag becomes the `MULTIPLE_TOTAL_CANDIDATES` soft
warning — the record loads, but the ambiguity is visible on it, not
silently resolved and forgotten.

---

## 7. A vendor's own system emits the literal string `"null"`

**Challenge.** `Invoice1653194348.pdf` (Gym Lounge) prints `GST NO : null`
— not a missing field, but the four-character word "null," apparently from
an unguarded string interpolation on the vendor's side. Naively, this reads
as a real value.

**Solution.** `_NULLISH = {"null", "none", "nan", "n/a", "na", "-", "--",
""}` is checked case-insensitively in two validators: optional string
fields fold to `"N/A"` (`_normalize_nullish_optional_str`), mandatory ones
raise (`_reject_nullish_mandatory_str`) so a `"null"` company name still
hard-fails rather than silently loading as the string "null." Tested
directly against the vendor's exact string in
`test_nullish_optional_strings_become_na`.

---

## 8. A 9% tax rate against a 0.00 tax amount, on the same line

**Challenge.** The same Gym Lounge invoice prints `SGST(9%): 0.00` and
`CGST(9%): 0.00` — a non-zero rate label beside a zero monetary amount.
Structurally valid (a waived or already-included tax is a real business
case), but worth flagging rather than silently trusting.

**Solution.** `dq_rules.rate_is_nonzero` parses the rate *label* (handling
composite labels like `"9%+9%"` without turning the field into a number —
it stays a string so multi-component rates survive intact) and
`ZERO_TAX_WITH_NONZERO_RATE` fires whenever a printed non-zero rate meets a
zero amount. Soft warning, not a hard fail — the record still loads.

---

## 9. One invoice number, printed two different ways in one PDF

**Challenge.** The Gym Lounge invoice number appears as
`Gym Lounge//2022-2023/240` in the header and
`Gym Lounge/ / 2022- 2023/ 240` in the payment log further down the same
document — inconsistent spacing from whatever templating produced the PDF.
Keying DynamoDB on the raw string would silently create two partitions for
one invoice, defeating the idempotent-write dedup the whole storage design
depends on.

**Solution.** `key_normalization.normalize()` uppercases, ASCII-folds, and
strips everything that isn't `[A-Z0-9]` before either string becomes part
of a key. Both spellings fold to the identical `GYMLOUNGE20222023240`. The
human-readable original is kept on the item for display —
`invoice_reference_number` is never overwritten, only the derived `SK` and
`dedup_key` are folded. `test_same_invoice_number_folds_to_one_key`
parametrizes both exact printings from the source file.

---

## 10. `validate_assignment=True` + an after-validator that mutates fields recurses infinitely

**Challenge.** The design calls for `ConfigDict(validate_assignment=True)`
(so a later `record.total_amount = x` re-validates) *and* a
`model_validator(mode="after")` that converts currency by reassigning
`self.total_amount = converted_value`. Under Pydantic 2.13, that
combination is not a stylistic wrinkle — it recurses until the interpreter
stack blows, because each reassignment inside the validator re-triggers
the same validator. Confirmed with a 6-line repro before touching the real
model:

```python
class M(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    a: Decimal
    @model_validator(mode="after")
    def v(self):
        self.a = self.a / 2   # RecursionError
        return self
```

**Solution.** `InvoiceRecord._assign()` (`models.py:203`) writes the
already-validated value directly through `self.__dict__[name]` and updates
`__pydantic_fields_set__`, bypassing the validator re-entry for that one
write while every *external* assignment (`record.mode_of_payment = "Card"`
from orchestration code) still validates normally.
`test_flags_are_not_duplicated_on_reassignment` exercises exactly that
external path to confirm the fix doesn't just avoid the crash but preserves
the feature it was protecting.

---

## 11. The plan's FX condition only converts `INR`, not "not GBP"

**Challenge.** `docs/plan.md`'s worked Pydantic snippet gates currency
conversion on `if self.original_currency.upper() == "INR"`. That matches
every current sample (all six are INR-denominated) but silently breaks the
stated invariant — "`currency` always ends up `GBP`" — for a hypothetical
USD or EUR invoice, which would pass through unconverted while still
claiming to be GBP.

**Solution.** Implemented the broader, invariant-preserving condition
instead: `if self.currency != fx.TARGET_CURRENCY`. Documented as a
deliberate deviation from the plan in `README.md`'s "Where this deviates"
section rather than left as a silent divergence, and
`test_gbp_records_are_not_reconverted` / the FX rate table's `USD_GBP` /
`EUR_GBP` entries exist specifically so this path is exercised even though
no current sample needs it.

---

## 12. boto3 rejects Python `float`, and Textract confidences are floats

**Challenge.** DynamoDB money fields are `Decimal` throughout — deliberately,
to avoid binary rounding — and `record.model_dump(mode="python")` passes
those straight through to `put_item` with no conversion layer. But
`extraction_confidence` values come back from Textract as native `float`
(`Confidence: 98.6`), and boto3's DynamoDB serializer raises `TypeError` on
`float` unconditionally, regardless of what else on the item is correctly
typed.

**Solution.** `_dynamo_safe()` (`dynamo_client.py:43`) recursively walks
the item and converts only `float` (via `Decimal(str(value))`, never
`Decimal(value)`, to avoid re-introducing binary imprecision) and
`date`/`datetime` (to ISO strings) — everything else, Decimals included,
passes through untouched.
`test_no_floats_survive_into_the_item` asserts no float reaches the built
item by walking it recursively, and confirms the confidence value survives
as `Decimal("98.6")`.

---

## 13. One Textract label maps to three canonical fields — the vendor alias cache's key couldn't be a `dict[str, str]`

**Challenge.** The vendor alias cache (`vendor_alias_cache.py`) exists to
skip a Bedrock call once a vendor's label vocabulary is known. The first
implementation stored `label -> canonical_field` as a flat one-to-one map.
But a single Textract label like `"SGST(9%):"` feeds *three* canonical
fields at once — `vat_tax_label`, `vat_tax_percentage`, and
`vat_tax_amount` — because the mapper's `source_labels` output can point
several canonical fields at the same source label. A one-to-one map
silently dropped two of the three on every cache hit.

**Solution.** Inverted the structure to `dict[str, list[str]]`
(`label_to_fields`), built by appending rather than overwriting
(`label_to_fields.setdefault(label, []).append(canonical)` in
`vendor_alias_cache.py:168`), and replayed by iterating every canonical
name a cached label maps to, not just the first.

---

## 14. `currency` broke the vendor cache's required-field coverage check

**Challenge.** Related to #13: the cache's read path refuses a "hit"
unless every *required* canonical field can be filled
(`company_name`, `buyer_name`, `invoice_reference_number`, `date`,
`currency`). But `currency` is usually inferred by the model from a symbol
(`Rs.`, `₹`) rather than copied from one labeled source field — it has no
`source_labels` entry to cache against, so the coverage check could never
be satisfied and the cache would defer to Bedrock on every single
document, defeating its purpose entirely.

**Solution.** Added a small, deliberately narrow `_CONSTANT_FIELDS =
("currency",)` set (`vendor_alias_cache.py:41`): fields whose *value*, not
just their label association, is reused across a vendor's invoices,
stored under `constants` in the cache entry and merged into the rebuilt
mapping before the coverage check runs. Kept to one field on purpose — the
comment on that constant spells out why nothing that varies invoice to
invoice belongs there.

---

## 15. Two-thirds of the image-only samples have no extractable ground truth

**Challenge.** `E-Receipt (2).pdf`, `Order_ID_7104598035.pdf`, and
`SALES RECEIPT_304743_1750688634308.pdf` are scans with no text layer, and
no OCR was run against them in this project (no Textract account was
exercised). There is no way to write a *real* Textract-response fixture
for these three without either fabricating values or running actual OCR —
and fabricating values while presenting them as extracted-from-the-file
would misrepresent what the fixture actually demonstrates.

**Solution.** Kept the three PDFs with genuine, verified ground truth
(VFS, Airtel, Gym Lounge — all confirmed against the real text layer with a
small parsing script) as the load-bearing fixtures, and built the other
three as **explicitly labeled synthetic fixtures**, each earning its
keep by exercising one path the real three don't: the clean happy path
(`E-Receipt (2)`, zero DQ flags), the `AnalyzeDocument` FORMS+TABLES
fallback and its Block-graph parser (`Order_ID_7104598035`), and the
quarantine path via a negative-total credit note
(`SALES RECEIPT_304743_...`). `tests/fixtures/README.md` states plainly
which three are real and which three are invented, so nobody mistakes a
synthetic value for evidence about the underlying scanned document.

---

## 16. Testing the pipeline without an AWS account

**Challenge.** Textract, Bedrock, and DynamoDB all require live AWS
credentials and billing. The pipeline needed to be runnable and
demonstrable — batch behavior, quarantine, idempotent dedup, DQ flags, all
of it — without either mocking so heavily that the tests stop meaning
anything, or requiring the person reading this repo to provision AWS
infrastructure first.

**Solution.** Two complementary layers:
- **`moto`** for the DynamoDB and Textract-shaped integration tests
  (`tests/integration/test_dynamo_mock.py`, `test_textract_mock.py`,
  `test_bedrock_mock.py`) — these exercise the real boto3 call shapes
  against an in-process fake AWS, not hand-rolled stubs.
- **A genuine offline replay mode** for end-to-end runs —
  `ReplayTextractClient` / `ReplayBedrockMapper`
  (`textract_client.py:191`, `bedrock_client.py:241`) implement the exact
  same interface as the real AWS clients and replay
  `data/silver/*.textract.json` / `*.mapped.json`. `scripts/seed_silver.py`
  seeds that silver layer from the committed fixtures on a clean checkout,
  so `python -m src.pipeline.run_batch --offline` runs the full 9-stage
  pipeline — including quarantine and duplicate-skip — with zero AWS calls
  and zero cost.

**Superseded (see challenge #19).** Once the pipeline was proven end to end
against real AWS, `--offline` was removed from `src/` entirely — the
project owner asked for exactly one production code path, always against
real infrastructure. The `moto` layer above is unaffected and still stands;
`ReplayTextractClient`, `ReplayBedrockMapper`, `InMemoryStore`, and
`scripts/seed_silver.py` no longer exist. `docs/tradeoffs.md` decisions #16
and #16b cover the reversal and what replaced the removed classes in the
test suite.

---

## 17. Batch stats need to run against a batch that was never in DynamoDB

**Challenge.** `src/stats/batch_stats.py` was designed to `scan_all()` a
live DynamoDB table. But an offline run (`--offline`) never touches
DynamoDB at all — `InMemoryStore` holds the records only for the life of
the process — so there was no way to compute aggregate stats for a batch
that had only ever run offline, which is the common case while developing
the mapping prompt.

**Solution.** Added `batch_stats.load_gold()`, which reads a
`data/gold/<batch>.jsonl` mirror back into the same item shape `compute()`
already expects, rehydrating money fields from string back to `Decimal` on
the way in (`_MONEY_ATTRS`). `compute()` itself stayed pure and
storage-agnostic throughout — it was already just aggregating a list of
dicts — so the fix was entirely about *sourcing* that list, not about the
aggregation logic. `--gold` was added as a `batch_stats` CLI flag
alongside `--month`, so both the live-table and offline-mirror paths are
first-class rather than one being a workaround.

---

## 18. Two silent confidence-lookup gaps, found on the first real document

**Challenge.** The first invoice ever run through this pipeline against
real, live AWS (`Order_ID_7104598035.pdf`, a Zomato food-delivery receipt —
not a fixture) mapped correctly, but `record.extraction_confidence` was
missing scores for three of its ten resolved fields: `product_name`,
`product_amount`, and `coupon_discount_amount`. Two distinct, unrelated
causes:

1. `resolve_confidences()` (`run_batch.py`) looked up Bedrock's reported
   `source_labels` against `ExtractedDocument.confidence_by_label()`, which
   keyed on Textract's *raw*, whitespace-stripped-only label text.
   Textract's real label for the discount line was
   `"-\nCoupon (TASTENEW)"` — an OCR'd bullet/dash on its own line ahead of
   the text. Bedrock, reasonably, reported the cleaned label
   `"Coupon (TASTENEW)"` back as its source citation. The exact-string
   lookup missed, silently — `resolve_confidences()` just omits a field it
   can't find rather than raising.
2. `confidence_by_label()` only ever walked
   `ExtractedDocument.summary_fields` — the header/footer label/value
   pairs. Values sourced from a line-item row (`product_name` /
   `product_amount`, both from the `Masala Soda` line's `Item` /
   `Unit Price` columns) had no lookup path at all, regardless of label
   matching, because `LineItem` never retained per-cell confidence in the
   first place — only one averaged, row-level `confidence` float.

**Solution.**
- Added `raw_shapes.normalize_label()`: collapses whitespace and strips
  leading bullet/dash decoration before comparison.
  `ExtractedDocument.confidence_by_label()` now keys on the normalized
  label, and `resolve_confidences()` normalizes its lookup key the same
  way, so both sides of the comparison agree regardless of incidental OCR
  formatting.
- Added `LineItem.field_confidences: dict[str, float]`, populated alongside
  `fields` in both parsers (`from_expense_response`'s line-item loop, and
  the FORMS+TABLES fallback's `_table_rows()`) from data Textract already
  returns per cell — previously collected only to be averaged into one
  row-level number and discarded.
- Added `ExtractedDocument.line_item_confidence_by_label()`, and
  `resolve_confidences()` now falls back to it when a label isn't found
  among summary fields. For a canonical field like `product_name` that can
  join several line-item rows into one semicolon-separated string
  (`docs/tradeoffs.md` #9), the confidence reported is the **minimum**
  across every contributing row, not an average — a weak row should not be
  allowed to hide behind stronger ones, which is exactly the case
  `LOW_FIELD_CONFIDENCE` exists to catch.

Both gaps were invisible to every prior test in this project, because every
hand-authored fixture's `source_labels` happened to match Textract's raw
text exactly, and no fixture's line items ever carried per-cell confidence
since the field didn't exist yet. Real, unscripted OCR output — not a
hand-written fixture — is what surfaced this.
`tests/unit/test_confidence_lookup.py` reproduces both exact real strings
from the document.

**A known limitation this exposed, not yet resolved.** The Textract output
already saved to `data/silver/2026/5/14/Order_ID_7104598035.textract.json`
was parsed and persisted *before* this fix existed, so its `line_items`
entries have empty `field_confidences` — the parsing fix only applies to
documents extracted after it landed. Re-running `resolve_confidences()`
against that stale file therefore still shows `product_name`/
`product_amount` as unresolved, correctly, since that snapshot genuinely
predates the fix. Confirming the fix end-to-end on this specific document
requires either re-calling Textract (a second, small real cost) or
re-deriving it from a cached raw API response, neither of which was done
here — the fix is verified by the dedicated unit tests
(`test_confidence_lookup.py`) using the exact real strings this document
produced, not by re-spending on a second live call.

---

## 19. `create_table_if_missing()` didn't wait for the table to finish creating

**Challenge.** The first real DynamoDB write in this project — storing the
validated Zomato-receipt record — was preceded by
`create_table_if_missing()` creating `invoice_records_dev` (with its two
GSIs) for the first time. The function called `resource.create_table(...)`
and returned immediately; nothing waited for the table to leave `CREATING`
and reach `ACTIVE`. Table creation with two GSIs took roughly 21 seconds
against the real account. Any `put_item` issued in that window would have
failed — not with an error pointing at the real cause, but with something
like `ResourceNotFoundException` or `ResourceInUseException`, on the very
first write anyone ever made against a fresh account.

**Solution.** Added
`resource.meta.client.get_waiter("table_exists").wait(TableName=self.table_name)`
immediately after `create_table(...)`, only on the branch where the table
didn't already exist (`dynamo_client.py:191`). Verified safe against moto
too — the mocked tests report a table as immediately `ACTIVE`, so the
waiter resolves instantly there and adds no meaningful time to the suite
(241 tests, unaffected).

This is a bug a mocked test suite structurally cannot catch on its own:
moto never models the real several-second table-creation delay, so every
test that called `create_table_if_missing()` against a mock always passed,
silently masking the race. It only surfaced on the first real write against
a genuinely fresh table — exactly the scenario this pipeline has to handle
correctly on day one of a real deployment, and exactly why this project's
approach has been to verify each stage against live AWS at least once
rather than trust the mocked suite as sufficient proof on its own.

---

## Test coverage as a running total

Every challenge above has at least one test named for the specific failure
mode it addresses, not a generic "handles edge case" test — for example
`test_zero_is_a_valid_business_value_not_a_missing_one`,
`test_same_invoice_number_folds_to_one_key`,
`test_garbled_buyer_name_is_preserved_not_rejected`,
`test_resolve_confidences_falls_back_to_line_item_columns`. The suite
currently stands at **241 tests**, split across `tests/unit/` (validators,
FX, key normalization, DQ rules, ingestion, quarantine, confidence lookup,
Textract pre-flight checks) and `tests/integration/` (Textract/Bedrock/
DynamoDB/S3 against moto, and the full batch orchestration against both the
local-file and S3-sourced discovery paths).

```bash
pytest        # 241 passed
```
