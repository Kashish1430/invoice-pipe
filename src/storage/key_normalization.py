"""Key normalization for DynamoDB PK/SK and the dedup key.

Exists because of the Gym Lounge sample: the same invoice number is printed as
`Gym Lounge//2022-2023/240` in one place and re-spaced differently in another
within a single document. Keying on the raw string would create two partitions
for one invoice and defeat the conditional-write dedup entirely.

The human-readable original is always kept on the item; only the key is folded.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize(value: str | None) -> str:
    """Uppercase, strip accents, drop everything that is not [A-Z0-9].

    >>> normalize("Gym Lounge//2022-2023/240")
    'GYMLOUNGE20222023240'
    >>> normalize("Gym  Lounge // 2022-2023 / 240")
    'GYMLOUNGE20222023240'
    """
    if value is None:
        return ""
    # NFKD then ASCII-fold so "Café" and "Cafe" land in the same partition.
    folded = unicodedata.normalize("NFKD", str(value))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("", folded.upper())


def vendor_pk(company_name: str) -> str:
    return f"VENDOR#{normalize(company_name)}"


def invoice_sk(invoice_reference_number: str) -> str:
    return f"INVOICE#{normalize(invoice_reference_number)}"


def dedup_key(company_name: str, invoice_reference_number: str) -> str:
    """Stored explicitly on the item, not just derivable from PK/SK.

    A GSI or a scan filter can then dedup without re-parsing the composite keys.
    """
    return f"{normalize(company_name)}|{normalize(invoice_reference_number)}"


def date_gsi1pk(iso_date: str) -> str:
    """`DATE#yyyy-mm` - month partitions keep reporting queries off full Scans."""
    return f"DATE#{iso_date[:7]}"


def date_gsi1sk(iso_date: str, pk: str) -> str:
    return f"{iso_date}#{pk}"


def status_gsi2pk(status: str) -> str:
    return f"STATUS#{status}"
