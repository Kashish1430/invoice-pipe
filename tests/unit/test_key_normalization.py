"""Key folding: the defence against the Gym Lounge re-spacing bug."""

from __future__ import annotations

import pytest

from src.storage.key_normalization import (
    date_gsi1pk,
    date_gsi1sk,
    dedup_key,
    invoice_sk,
    normalize,
    status_gsi2pk,
    vendor_pk,
)


@pytest.mark.parametrize(
    "raw",
    [
        "Gym Lounge//2022-2023/240",
        "Gym Lounge/ / 2022- 2023/ 240",   # the same number, as reprinted lower down
        "GYM LOUNGE // 2022 - 2023 / 240",
        "  gym lounge//2022-2023/240  ",
    ],
)
def test_same_invoice_number_folds_to_one_key(raw):
    """One document prints this number two ways; both must hit one partition."""
    assert normalize(raw) == "GYMLOUNGE20222023240"


def test_accents_fold_to_ascii():
    assert normalize("Café Móvil") == normalize("Cafe Movil") == "CAFEMOVIL"


def test_none_and_empty_are_safe():
    assert normalize(None) == ""
    assert normalize("") == ""


def test_distinct_vendors_stay_distinct():
    assert normalize("Gym Lounge") != normalize("Gym Lounge 2")


def test_key_prefixes():
    assert vendor_pk("Gym Lounge") == "VENDOR#GYMLOUNGE"
    assert invoice_sk("Gym Lounge//2022-2023/240") == "INVOICE#GYMLOUNGE20222023240"
    assert status_gsi2pk("LOADED") == "STATUS#LOADED"


def test_dedup_key_matches_the_documented_shape():
    assert (
        dedup_key("Gym Lounge", "Gym Lounge//2022-2023/240")
        == "GYMLOUNGE|GYMLOUNGE20222023240"
    )


def test_dedup_key_is_stable_across_reprintings():
    assert dedup_key("Gym Lounge", "Gym Lounge//2022-2023/240") == dedup_key(
        "GYM  LOUNGE", "Gym Lounge/ / 2022- 2023/ 240"
    )


def test_date_index_keys_partition_by_month():
    assert date_gsi1pk("2022-05-22") == "DATE#2022-05"
    assert (
        date_gsi1sk("2022-05-22", "VENDOR#GYMLOUNGE")
        == "2022-05-22#VENDOR#GYMLOUNGE"
    )


def test_gsi1_sort_keys_order_chronologically():
    keys = [
        date_gsi1sk("2022-05-22", "VENDOR#B"),
        date_gsi1sk("2022-05-02", "VENDOR#A"),
        date_gsi1sk("2022-05-19", "VENDOR#C"),
    ]
    assert sorted(keys) == [
        "2022-05-02#VENDOR#A",
        "2022-05-19#VENDOR#C",
        "2022-05-22#VENDOR#B",
    ]
