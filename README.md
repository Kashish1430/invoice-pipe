# invoice_pipe

[![Tests](https://github.com/Kashish1430/invoice-pipe/actions/workflows/tests.yml/badge.svg)](https://github.com/Kashish1430/invoice-pipe/actions/workflows/tests.yml)

A daily-batch pipeline that turns arbitrary multi-vendor PDF invoices into
typed, FX-normalised, deduplicated records in DynamoDB, using AWS Textract
for OCR and Claude (via Amazon Bedrock) for the one step OCR can't do:
deciding what a vendor's own vocabulary actually *means*.

**Status:** production code path, no mock/offline mode. Verified end-to-end
against real AWS on 3 of 6 sample invoices — see [Verified against real
AWS](#verified-against-real-aws) below for the actual results.

## How this was built
Written over a few days with Claude Code and stayed local. The architecture,
the trade-off calls in docs/trade-offs.md, and every bug in docs/challenges.md 
are mine, what I didn't do by hand is the typing. Happy to walk through any 
decision in here and why the alternative lost.

---

## The problem this solves

Every vendor labels their own invoice differently. One prints `Grand Total`,
another `Total Amount Payable`, a third just `Total`. One invoice has a single
line item; another has six competing figures that could all plausibly be "the
total." Textract's OCR is excellent at finding *every* label and value on a
page — it has no way to know which one your schema actually means, or that
`SGST(9%): 0.00` is a rate charged against a waived amount, not a missing
value.

This pipeline splits that problem into two stages built for it: **Textract**
extracts everything, generically, with no knowledge of any target schema;
**Bedrock** (Claude, forced tool-use) maps that output onto a fixed 13-field
canonical schema, citing which source label it took each value from and
flagging when it had to choose between competing candidates. Pydantic then
validates, converts currency, and flags data-quality concerns before a
conditional write lands the record in DynamoDB — idempotently, so re-running
a batch is always safe.

## Architecture

```
S3 (s3://<bucket>/<yyyy>/<mm>/<dd>/*.pdf)     data/raw/*.pdf
  or a local directory                    ─┬─
                                            │
                                            ▼
                              Textract (AnalyzeExpense,
                              FORMS+TABLES fallback)  ──▶ data/silver/*.textract.json
                                            │
                                            ▼
                              Bedrock (Converse,
                              forced tool use)         ──▶ data/silver/*.mapped.json
                                            │
                                            ▼
                              Pydantic (coerce → domain
                              check → FX → DQ flags)
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                            ▼
                     DynamoDB (conditional put)     hard failure
                       ──▶ data/gold/<batch>.jsonl    ──▶ data/quarantine/*.error.json
                                            │
                                            ▼
                              DynamoDB ──▶ natural-language query
                              (a separate read path -- see "Natural-language
                              queries" below; nothing above this line changes
                              to support it)
```

Full design rationale — the trade-off analysis behind each stage — lives in
[`docs/plan.md`](docs/plan.md).

## Quickstart

This pipeline always talks to real AWS. It needs credentials with Textract,
Bedrock, and DynamoDB access (the standard boto3 chain: environment
variables, `~/.aws/credentials`, or an execution role) and the bucket/table
named in `config/settings.yaml`.

```bash
python -m venv venv && ./venv/Scripts/pip install -e ".[dev]"

pytest                             # 251 tests, no AWS required or touched
python -m src.pipeline.run_batch   # processes data/raw/*.pdf against real AWS
```

`DynamoStore.create_table_if_missing()` creates the table and both GSIs on
first use, and waits for the table to actually finish creating — several
seconds, with GSIs — before the first write, so a brand-new account can't
race its own first `put_item`.

Re-running a batch reports `DUPLICATE_SKIPPED` for anything already loaded
and re-quarantines anything that still hard-fails — the conditional write
makes reprocessing safe by construction.

### Sourced from S3 instead of a local directory

```bash
python -m src.pipeline.run_batch --from-s3
python -m src.pipeline.run_batch --from-s3 --batch-date 2026-05-14
python -m src.pipeline.run_batch --from-s3 --bucket my-other-bucket
```

Treats S3 as the canonical raw store, partitioned by business date at
`s3://<bucket>/<yyyy>/<mm>/<dd>/*.pdf`. Files are read directly where they
already live — nothing gets re-uploaded. The date defaults to `date.today()`;
`pipeline.batch_date_override` in `config/settings.yaml` (or `--batch-date`
per-invocation) pins it to a specific day for backfills.

### Stats

```bash
python -m src.stats.batch_stats --month 2026-04
python -m src.stats.batch_stats --gold data/gold/<batch-id>.jsonl
```

### Natural-language queries

```bash
python -m src.query.nl_query "how many invoices are quarantined?"
python -m src.query.nl_query "what's the total from Gym Lounge?"
```

Ask a plain-English question, get a plain-English answer, read against the
real DynamoDB table. Two Bedrock calls, not one raw query: the first
(forced tool use, same pattern as the mapping stage) picks *which* of a
small, fixed set of safe, pre-built read operations answers the question
(`scan_all` / `query_status` via GSI2 / `query_month` via GSI1) and with
what parameters -- the model never writes a DynamoDB expression itself,
only chooses among operations `DynamoStore` already exposes elsewhere in
this codebase. Any counting or summing runs in plain Python over `Decimal`,
never in the model, for the same reason `InvoiceRecord` never lets Bedrock
do money math. The second call phrases the computed result into a sentence,
using only the numbers Python actually produced.

`src/query/nl_query.py`; not part of the batch pipeline and not reachable
from `run_batch` -- a separate, interactive read path over the same table.

---

## Verified against real AWS

Not synthetic examples — these are the actual results of running three of
the six sample invoices through live Textract and Bedrock. Being precise
about scope: only the first one has also gone through a real DynamoDB
write; the other two are verified through Pydantic (confirmed `LOADED`
locally) but not yet stored for real.

**`Order_ID_7104598035.pdf`** (a Zomato food-delivery receipt) — the one
taken all the way through DynamoDB and read back:

| Stage | Result |
|---|---|
| Discovery | `s3://invoice-pipe/raw/2026/5/14/Order_ID_7104598035.pdf` |
| Textract | `AnalyzeExpense`, 31 label/value pairs, 1 line item, no fallback needed |
| Bedrock | `eu.anthropic.claude-haiku-4-5`, 5,168 input / 825 output tokens |
| Pydantic | `LOADED`, 10/10 fields confidence-scored, FX round-trip verified reversible |
| DynamoDB | `PK=VENDOR#COFFEECULTURE`, `SK=INVOICE#7104598035`, read back through both GSIs |

Bedrock reconciled the total from five separate, unlabeled-as-such charges —
item cost, delivery, platform fee, surge fee, packaging — minus a coupon,
none of which was individually marked "the total":

```
₹175.00 (item) + ₹49 (delivery) + ₹10 (platform fee) + ₹15 (surge)
  + ₹30 (packaging) + ₹9.43 (tax) − ₹52.50 (coupon) = ₹235.93 → £1.89 GBP
```

**`E-Receipt (2).pdf`** (a Trip.com flight booking) and
**`SALES RECEIPT_304743_1750688634308.pdf`** (a Sephra Europe chocolate
order) — real Textract + Bedrock, validated `LOADED` with zero DQ flags,
not yet written to DynamoDB:

| | E-Receipt (2) | SALES RECEIPT |
|---|---|---|
| Company | Trip.com Travel Singapore Pte. Ltd. | Sephra Europe Ltd (CFW) |
| Total | £607.40 (already GBP) | £19.60 (already GBP) |
| Bedrock tokens | 4,274 in / 719 out | 6,726 in / 800 out |

Both turned out to be **GBP-native**, not INR like the other four samples —
`docs/plan.md`'s original premise that all six are INR-denominated was
wrong about these two. `E-Receipt (2)`'s real date, `"October 6, 2025"`,
also exposed a genuine gap in the accepted date formats (`%B %d, %Y` was
missing) — found the same way every other issue in this project was: by
running real data through the pipeline, not by guessing at edge cases.

Real cost for all three documents, every AWS call included: **≈$0.06.**

Real bugs were caught by pushing this through live AWS rather than trusting
the mocked test suite alone — a confidence-lookup gap that only a real
document's exact OCR text exposed, a DynamoDB table-creation race condition
that no mock could ever reproduce (mocks don't model AWS's actual
multi-second table-creation delay), and the missing date format above.
Written up in [`docs/challenges.md`](docs/challenges.md) #18–19.

**Natural-language query, against the live table** — same Zomato record,
asked in plain English rather than looked up by key:

```
$ python -m src.query.nl_query "What is the total from Coffee Culture?"
The total from Coffee Culture is £1.89.
```

Bedrock chose `scan_all` + `vendor_contains="Coffee Culture"` +
`aggregation=sum_total_amount` on its own — the question names neither a
status nor a month, so a full scan with a vendor filter was the reasonable
call, not a hardcoded mapping for this specific question. £1.89 is exactly
the converted total from the row above; Python computed it, Bedrock only
phrased it.

---

## Testing

**251 tests, zero AWS cost, zero AWS calls.** `moto` mocks S3 and DynamoDB
fully; hand-written fakes (`FakeTextract`, `FakeBedrock`) script Textract's
and Bedrock's real async/retry contracts, since moto doesn't cover either
service. All of this is test-only — nothing in `src/` or the CLI can reach
it; the production pipeline has exactly one path, against real
infrastructure, whether it's processing 1 file or 500.

| Stage | Tests | What's checked |
|---|---:|---|
| Validation (`InvoiceRecord`) | 103 | money/date coercion, domain rules, FX conversion, the 12-rule DQ table |
| Discovery (local + S3) | 34 | file walking, S3 date-partition listing, batch-date resolution, local mirroring |
| Full batch orchestration | 34 | per-file isolation, dedup, quarantine, gold mirror, stats, local mirroring |
| Storage (DynamoDB) | 25 | item shape, conditional writes, both GSIs, key normalization |
| Extraction (Textract) | 18 | async polling/pagination, FORMS+TABLES fallback, size limits |
| Mapping (Bedrock) | 16 | forced tool use, retry/escalation, vendor alias cache |
| Quarantine/errors | 12 | sidecar rule attribution, serialization safety |
| Natural-language query | 9 | operation selection, Python-side aggregation, vendor filter, quarantine exclusion |

```bash
pytest -q
```

## Configuration

Everything environment-specific lives in `config/`; nothing under `src/`
hardcodes a region, table name, or threshold.

| File | Holds |
|---|---|
| `settings.yaml` | AWS region, S3 buckets, table name, confidence thresholds, model IDs, batch-date override |
| `fx_rates.yaml` | source-units-per-GBP rates, plus the `as_of` date stamped onto every record |
| `canonical_schema.yaml` | the 13 canonical fields and their semantics — the single source for both Bedrock's tool schema and its system prompt, so they can't drift apart |

`INVOICE_PIPE_SETTINGS`, `INVOICE_PIPE_FX_RATES`, and
`INVOICE_PIPE_CANONICAL_SCHEMA` env vars override these paths (used by the
test suite to run against an isolated temp tree).

## Project layout

```
config/           settings, FX rates, canonical field semantics
data/
  raw/            source PDFs -- immutable, never moved even on failure
  silver/         per-file Textract + mapped JSON, for debug/audit
  gold/           JSONL mirror of what was loaded, keyed by dedup_key
  quarantine/     <filename>.error.json sidecars for hard-failed files
docs/
  plan.md         architecture and trade-off analysis (the original design)
  tradeoffs.md    18 architectural decisions -- alternatives weighed, why each won
  challenges.md   19 real problems hit building this, and the fix for each
src/
  ingestion/      file_discovery, s3_discovery, s3_uploader
  extraction/     textract_client, raw_shapes
  mapping/        bedrock_client, field_semantics, vendor_alias_cache
  validation/     models (InvoiceRecord), fx, dq_rules
  storage/        dynamo_client, key_normalization
  quarantine/     sidecar_writer
  stats/          batch_stats
  query/          nl_query -- natural-language questions over the live table
  pipeline/       run_batch (orchestrator), errors
tests/            unit/, integration/, fixtures/ -- moto + fakes, no real AWS
```

## Further reading

- **[`docs/plan.md`](docs/plan.md)** — the original architecture spec: the
  Textract-vs-vision-LLM trade-off, the Pydantic validation design, the
  DynamoDB key/index design, and the pipeline diagram.
- **[`docs/tradeoffs.md`](docs/tradeoffs.md)** — every architectural decision
  made, the alternatives actually weighed, and why each one won — including
  the two that got reversed later (offline mode, added then removed once the
  pipeline was proven against real AWS).
- **[`docs/challenges.md`](docs/challenges.md)** — 19 concrete problems hit
  while building this, traced to a real sample PDF or a real AWS call, with
  the fix and the test that pins it down. Not hypothetical edge cases.
- **[`tests/fixtures/README.md`](tests/fixtures/README.md)** — all six named
  fixtures are now grounded in real evidence (PDF text layer, or a real
  Textract + Bedrock call); two small, honestly-named auxiliary fixtures
  cover code paths none of the six real documents happens to exercise.

## What the real samples drove

Every non-obvious design decision traces to something specific in the actual
sample invoices, not a hypothetical edge case:

- **VFS visa invoice** — a corrupted font mapping garbles the buyer's name to
  unreadable bytes, digits arrive kerning-split (`1 1 1,333.00`), and
  `Grand Total: 0.00` contradicts a non-zero `Amount:` elsewhere on the page.
  Drives the money-coercion validator, the `>= 0`-not-`> 0` domain rule, and
  passing garbled text through rather than guessing at it.
- **Airtel statement (6pp)** — six competing "total"-like figures and four
  competing dates on one document. This is the actual reason a language
  model is in the pipeline at all — extraction alone cannot pick a winner.
- **Gym Lounge invoice** — a literal `GST NO : null` (the vendor's own system
  printed the word), a 9% tax rate charged against a 0.00 amount, and one
  invoice number printed two different ways in the same document. Drives
  nullish-string handling and the key-normalization that prevents duplicate
  DynamoDB partitions for one invoice.
- **Three image-only scans** — no embedded text layer at all, which is why
  Textract runs uniformly against the rendered page raster rather than
  branching on whether a text layer happens to exist. Real Textract + Bedrock
  calls against all three (see [Verified against real
  AWS](#verified-against-real-aws)) later confirmed the point from the other
  angle: a Zomato receipt whose total is built from five separate charges and
  a coupon with no single line marked "total," and two GBP-native purchases
  that the original brief's INR-only premise didn't anticipate at all.

## Current status & known gaps

- **4 of the 10 sample PDFs referenced in the original brief are missing** —
  only 6 ever existed. Vendor variance is the whole point of this project, so
  the mapping prompt and DQ rules need re-validating once the remaining 4
  land.
- **3 of the 6 samples currently exist anywhere accessible to the
  pipeline** — `Order_ID_7104598035.pdf`, `E-Receipt (2).pdf`, and
  `SALES RECEIPT_304743_1750688634308.pdf`, all uploaded to
  `s3://invoice-pipe/raw/2026/5/14/` and verified through real Textract +
  Bedrock (see [Verified against real AWS](#verified-against-real-aws)
  above; only the first has also been written to real DynamoDB).
  `data/raw/` holds local mirrors of those 3 automatically (`--from-s3`'s
  default `mirror_local=True`); the other 3 samples were removed from local
  disk and are held in a secure backup outside this repo, not yet
  re-uploaded to S3. `--from-s3` will pick up all 6 in one run once they are.
- **All 6 named test fixtures are now real** — grounded either in the
  source PDF's own text layer, or in a real captured Textract + Bedrock
  call for the 3 that were originally image-only scans. Two small,
  explicitly synthetic auxiliary fixtures (`_forms_fallback_sample`,
  `_negative_total_credit_note`) exist purely to keep test coverage for two
  code paths that none of the 6 real documents happens to exercise — see
  `tests/fixtures/README.md` for exactly what each one is for and why.
- **FX is a fixed rate**, not date-keyed. `fx_rate_applied` / `fx_rate_date`
  are stamped on every record so switching to a live rates API later stays
  auditable against what was already loaded.
- **Stats are a scan-and-compute job**, not Athena. The migration trigger —
  ~1M items, or a need for sub-minute freshness — is a literal field
  (`exceeds_migration_threshold`) computed on every stats run, not a
  judgment call made from memory later.
- **The natural-language query agent has no `docs/tradeoffs.md` /
  `docs/challenges.md` entry yet** — built and tested (9 tests, `moto` +
  fake Bedrock) under real time pressure, verified once against real
  Bedrock + the live table (see [Verified against real
  AWS](#verified-against-real-aws)), but not documented to the same depth
  as the rest of the pipeline. Vendor matching is a plain case-insensitive
  substring, not fuzzy — "gym" won't match "Gymnasium Co." Two Bedrock
  calls per question roughly doubles the cost/latency of a single mapping
  call; fine for an interactive tool, not something to put in the daily
  batch loop.
- **CI is wired up** (`.github/workflows/tests.yml`, badge above) but is a
  single Python-3.12/Ubuntu job — no version matrix, no lint/type-check
  step, just the test suite on every push and PR.
