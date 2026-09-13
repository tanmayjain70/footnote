"""The registry and the value parser.

``parse_value`` is the one reader of a value for both the model's answers
and the reviewer's corrections, so the forms it accepts are spelled out here
case by case -- and so are the ones it refuses, because a parser that guesses
"ten years" is 10 would also guess something wrong one day.
"""

from __future__ import annotations

import re

import pytest

from app.models.enums import ValueIssue
from app.services.extraction_fields import (
    FIELD_BY_KEY,
    FIELDS,
    SCHEMA_VERSION,
    FieldSpec,
    field_by_key,
    fields_as_dicts,
    parse_value,
)

EXPECTED_KEYS = [
    "tenant_name",
    "landlord_name",
    "property_address",
    "unit",
    "term_start",
    "term_end",
    "term_years",
    "annual_rent_gbp",
    "rent_review_basis",
    "rent_review_date",
    "break_date",
    "break_notice_months",
    "repairing_obligation",
    "permitted_use",
    "deposit_gbp",
    "guarantor",
    "vat_elected",
]
EXPECTED_TYPES = {
    "tenant_name": "text",
    "landlord_name": "text",
    "property_address": "text",
    "unit": "text",
    "term_start": "date",
    "term_end": "date",
    "term_years": "integer",
    "annual_rent_gbp": "money",
    "rent_review_basis": "enum",
    "rent_review_date": "date",
    "break_date": "date",
    "break_notice_months": "integer",
    "repairing_obligation": "enum",
    "permitted_use": "text",
    "deposit_gbp": "money",
    "guarantor": "text",
    "vat_elected": "bool",
}


def spec(key: str) -> FieldSpec:
    return FIELD_BY_KEY[key]


# --------------------------------------------------------------- registry --


def test_the_registry_has_the_seventeen_fields_in_order():
    assert [f.key for f in FIELDS] == EXPECTED_KEYS
    assert {f.key: f.type for f in FIELDS} == EXPECTED_TYPES
    assert SCHEMA_VERSION == 1
    assert set(FIELD_BY_KEY) == set(EXPECTED_KEYS)
    assert field_by_key("term_end") is FIELD_BY_KEY["term_end"]
    assert field_by_key("no_such_field") is None


def test_every_field_is_described_for_a_model():
    for f in FIELDS:
        assert f.label, f.key
        assert len(f.description) > 40, f.key
        if f.type == "enum":
            assert f.enum_values, f.key
        else:
            assert f.enum_values == (), f.key
    assert spec("rent_review_basis").enum_values == (
        "open_market",
        "rpi",
        "cpi",
        "fixed_uplift",
        "none",
    )
    assert spec("repairing_obligation").enum_values == (
        "full_repairing",
        "internal_repairing",
        "landlord_repairing",
    )


def test_every_stub_pattern_compiles_and_captures_a_value():
    for f in FIELDS:
        pattern = re.compile(f.stub_pattern)
        assert "value" in pattern.groupindex, f.key


def test_fields_as_dicts_is_the_public_shape():
    published = fields_as_dicts()
    assert [item["key"] for item in published] == EXPECTED_KEYS
    for item in published:
        assert set(item) == {"key", "label", "type", "description", "enum_values"}
        assert isinstance(item["enum_values"], list)
    basis = next(item for item in published if item["key"] == "rent_review_basis")
    assert "open_market" in basis["enum_values"]


def test_field_spec_is_frozen():
    with pytest.raises(AttributeError):
        spec("unit").label = "Something else"  # type: ignore[misc]


# ------------------------------------------------------------------ dates --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 April 2024", "2024-04-01"),
        ("1st April 2024", "2024-04-01"),
        ("22nd June 2019", "2019-06-22"),
        ("3rd March 2031", "2031-03-03"),
        ("29 February 2028", "2028-02-29"),
        ("01/04/2024", "2024-04-01"),
        ("13/04/2024", "2024-04-13"),
        ("01.04.2024", "2024-04-01"),
        ("2024-04-01", "2024-04-01"),
        ("2024-04-01T00:00:00", "2024-04-01"),
        ("April 1, 2024", "2024-04-01"),
        ("1 Apr 2024", "2024-04-01"),
        ("on 1 April 2024", "2024-04-01"),
        ('"31 March 2034".', "2034-03-31"),
    ],
)
def test_dates_are_read_day_first_and_returned_as_iso(text, expected):
    assert parse_value(spec("term_start"), text) == (expected, None)


@pytest.mark.parametrize(
    "text",
    ["sometime next spring", "the third anniversary", "13/13/2024", "30 February 2024", "2024", ""],
)
def test_dates_that_are_not_dates_are_unparseable(text):
    assert parse_value(spec("break_date"), text) == (None, ValueIssue.UNPARSEABLE)
    assert parse_value(spec("break_date"), text)[1] == "unparseable"


def test_an_american_date_is_not_guessed():
    """04/01/2024 is the fourth of January here; nothing reads it as April."""
    assert parse_value(spec("term_end"), "04/01/2024") == ("2024-01-04", None)


# ------------------------------------------------------------------ money --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("£42,500", 42500.0),
        ("42,500.00 per annum", 42500.0),
        ("£41,000 per annum", 41000.0),
        ("£1,000", 1000.0),
        ("£12,500 plus VAT", 12500.0),
        ("£173,250 (one hundred and seventy-three thousand) per annum.", None),
        ("GBP 9,750 pa", 9750.0),
        ("8000", 8000.0),
        ("£24,350.50", 24350.5),
    ],
)
def test_money_strips_the_noise_and_keeps_the_number(text, expected):
    value, issue = parse_value(spec("annual_rent_gbp"), text)
    if expected is None:
        assert (value, issue) == (None, ValueIssue.UNPARSEABLE)
    else:
        assert (value, issue) == (expected, None)
        assert isinstance(value, float)


@pytest.mark.parametrize("text", ["forty-two thousand pounds", "£", "TBC", "£42,500 to £45,000"])
def test_money_without_a_single_number_is_unparseable(text):
    assert parse_value(spec("deposit_gbp"), text) == (None, ValueIssue.UNPARSEABLE)


# --------------------------------------------------------------- integers --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10", 10),
        ("6", 6),
        ("6 months", 6),
        ("6 months'", 6),
        ("10 years", 10),
        ("12 calendar months", 12),
    ],
)
def test_integers_accept_digits_with_an_optional_unit(text, expected):
    value, issue = parse_value(spec("break_notice_months"), text)
    assert (value, issue) == (expected, None)
    assert isinstance(value, int) and not isinstance(value, bool)


@pytest.mark.parametrize("text", ["ten years", "ten-ish", "10.5", "six months", "3 to 6 months"])
def test_integers_in_words_are_unparseable(text):
    assert parse_value(spec("term_years"), text) == (None, ValueIssue.UNPARSEABLE)


# ------------------------------------------------------------------ bools --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", True),
        ("Yes.", True),
        ("true", True),
        ("elected", True),
        ("has elected", True),
        ("opted to tax", True),
        ("no", False),
        ("No", False),
        ("false", False),
        ("not elected", False),
        ("has not elected", False),
    ],
)
def test_bools_from_the_words_a_lease_uses(text, expected):
    assert parse_value(spec("vat_elected"), text) == (expected, None)


@pytest.mark.parametrize("text", ["maybe", "elected not", "VAT", ""])
def test_bools_that_are_not_clear_are_unparseable(text):
    assert parse_value(spec("vat_elected"), text) == (None, ValueIssue.UNPARSEABLE)


# ------------------------------------------------------------------ enums --


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        ("rent_review_basis", "open market", "open_market"),
        ("rent_review_basis", "Open Market", "open_market"),
        ("rent_review_basis", "open-market", "open_market"),
        ("rent_review_basis", "OPEN_MARKET", "open_market"),
        ("rent_review_basis", "RPI", "rpi"),
        ("rent_review_basis", "fixed uplift", "fixed_uplift"),
        ("rent_review_basis", "none", "none"),
        ("repairing_obligation", "Internal repairing", "internal_repairing"),
        ("repairing_obligation", "full repairing.", "full_repairing"),
        ("repairing_obligation", "landlord-repairing", "landlord_repairing"),
    ],
)
def test_enums_are_matched_case_and_separator_insensitively(key, text, expected):
    assert parse_value(spec(key), text) == (expected, None)


@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("rent_review_basis", "vibes"),
        ("rent_review_basis", "market"),
        ("repairing_obligation", "full"),
        ("repairing_obligation", "open market"),
        ("repairing_obligation", ""),
    ],
)
def test_enums_outside_the_list_are_unparseable(key, text):
    assert parse_value(spec(key), text) == (None, ValueIssue.UNPARSEABLE)


# ------------------------------------------------------------------- text --


def test_text_is_stripped_and_never_empty():
    assert parse_value(spec("tenant_name"), "  Acme Limited ") == ("Acme Limited", None)
    assert parse_value(spec("tenant_name"), '"Acme Limited".') == ("Acme Limited", None)
    assert parse_value(spec("unit"), "Unit  4") == ("Unit 4", None)
    assert parse_value(spec("guarantor"), "   ") == (None, ValueIssue.UNPARSEABLE)
    assert parse_value(spec("guarantor"), None) == (None, ValueIssue.UNPARSEABLE)
