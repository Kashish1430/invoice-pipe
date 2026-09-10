# Fixtures

## `textract_responses/`

Raw AWS envelopes, in the exact shape the SDK returns them:

* six files (one per real sample) are `GetExpenseAnalysis` responses
  (`ExpenseDocuments`);
* `_forms_fallback_sample.json` is a `GetDocumentAnalysis` response
  (`Blocks`) -- see "Two purely synthetic auxiliary fixtures" below.

## `mapped/`

The tool input Bedrock produces for each document -- the *semantic
decision*: which of Airtel's six "total"-like figures is the amount payable,
that Gym Lounge's `Sub Total` is post-discount, that a garbled buyer name is
passed through rather than guessed. For the six real samples this is Bedrock's
actual real output, captured from a live Converse call, not authored by hand.

## Provenance -- all six real samples are now real, end to end

Every one of the six named fixtures is grounded in genuine evidence: the
source PDF's own text layer, or -- for the three that had none -- real
Textract OCR and real Bedrock mapping, both called live and captured
verbatim. **None of the six is hand-invented data any more.**

| Fixture | Grounded in |
|---|---|
| `2324GBRAMD125920` | real PDF text layer -- corrupted-cmap buyer name, `1 1 1,333.00` kerning split, `Grand Total: 0.00` against `Amount: 1,333.00` |
| `7042968270_543523577_4_2026` | real PDF text layer -- six competing totals, statement/bill/due dates, `698.00 + 107.82 = 805.82` |
| `Invoice1653194348` | real PDF text layer -- literal `GST NO : null`, `SGST(9%): 0.00`, and the invoice number printed both as `Gym Lounge//2022-2023/240` and `Gym Lounge/ / 2022- 2023/ 240` |
| `Order_ID_7104598035` | **real Textract + Bedrock call** -- a Zomato food-delivery receipt (Coffee Culture), ₹235.93 built from five separate charges minus a coupon |
| `E-Receipt (2)` | **real Textract + Bedrock call** -- a Trip.com flight booking receipt, £607.40, already GBP-denominated |
| `SALES RECEIPT_304743_1750688634308` | **real Textract + Bedrock call** -- a Sephra Europe chocolate order, £19.60, already GBP-denominated |

The `textract_responses/*.json` envelopes for the three "real Textract +
Bedrock call" rows are not the literal bytes AWS returned -- those were never
saved, only the already-parsed `ExtractedDocument`. They are a
**verified-lossless reconstruction**: every summary field, line item, and
confidence score round-trips exactly back through `from_expense_response()`
to the real parsed result (checked field-by-field before these files were
written, not assumed). The `mapped/*.mapped.json` files for these three are
the real Bedrock output directly, no reconstruction needed.

Two real findings this correction surfaced, worth knowing:
- All six samples were originally assumed INR-denominated
  (`docs/plan.md`'s preface). Two of the six -- `E-Receipt (2)` and
  `SALES RECEIPT_304743_1750688634308` -- turned out to be **GBP-native**
  once real data existed for them.
- `E-Receipt (2)`'s real date, `"October 6, 2025"`, exposed a genuine gap in
  `InvoiceRecord`'s accepted date formats (`"%B %d, %Y"` was missing) --
  found and fixed the same way every other issue in this project was: by
  running real data through the pipeline, not by guessing at edge cases.

## Two purely synthetic auxiliary fixtures

Once all six real samples were replaced with real captured data, none of
them naturally exercises two specific code paths any more -- the FORMS+TABLES
fallback, and the quarantine/negative-money hard-fail. Rather than force a
real document to pretend to be something it isn't, two small, explicitly
synthetic fixtures were added instead, under honest names that don't claim
to represent any real invoice:

| Fixture | Exercises | Why no real document can |
|---|---|---|
| `_forms_fallback_sample` | the FORMS+TABLES fallback parser | the real `Order_ID_7104598035` Textract call succeeded via `AnalyzeExpense` on the first try -- no real sample here ever needed the fallback |
| `_negative_total_credit_note` | the quarantine path -- a credit note whose total is negative, rejected by the `>= 0` domain rule | none of the six real documents has a negative total; this scenario is invented specifically to keep hard-fail/quarantine coverage in `test_pipeline.py` |

Neither is loaded by `textract_response()`/`mapped_fixture()` under a real
invoice's name, and neither is presented anywhere as evidence about a real
document -- their whole purpose is to be visibly, honestly synthetic.
