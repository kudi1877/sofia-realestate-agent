"""Tests for canonical neighborhood names (TIN-468 part 2)."""

import pytest

from src.utils.neighborhoods import canonicalize_neighborhood


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Latin slugs from imoti.net → Cyrillic
        ("Bankja", "Банкя"),
        ("Malinova Dolina", "Малинова долина"),
        ("Lulin 5", "Люлин 5"),
        ("Sofia Center", "Център"),
        ("Sofija Studentski Grad", "Студентски град"),
        ("Studentski Grad", "Студентски град"),
        ("Gotse Delchev", "Гоце Делчев"),
        ("Drujba 2", "Дружба 2"),
        ("Ovcha Kupel 2", "Овча купел 2"),
        ("Vitosha", "Витоша"),
        ("Dragalevci", "Драгалевци"),
        ("Obelja 2", "Обеля 2"),
        # Prefix stripping — same place, one group
        ("гр. Банкя", "Банкя"),
        ("с. Лозен", "Лозен"),
        ("ж.к. Люлин 3", "Люлин 3"),
        ("кв. Бояна", "Бояна"),
        # Already-canonical Cyrillic passes through
        ("Малинова долина", "Малинова долина"),
        ("Гоце Делчев", "Гоце Делчев"),
        ("Люлин 5", "Люлин 5"),
        # Empty / missing
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_canonicalize(raw, expected):
    assert canonicalize_neighborhood(raw) == expected


def test_latin_and_cyrillic_variants_converge():
    # The core property: every spelling of the same place → one group key.
    variants = ["Bankja", "гр. Банкя", "Банкя", "bankja"]
    assert len({canonicalize_neighborhood(v) for v in variants}) == 1


def test_idempotent():
    # Repairing the DB and re-scraping must not drift names further.
    for name in ["Bankja", "гр. Банкя", "Malinova Dolina", "Хаджи Димитър"]:
        once = canonicalize_neighborhood(name)
        assert canonicalize_neighborhood(once) == once
