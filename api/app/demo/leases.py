"""Generated leases for the demo, the tests and the evaluation set.

Nothing here is real: Hallam & Pryce, the landlords, the tenants and the
buildings are invented, though the streets and towns are Manchester's. What
matters is that every lease carries a known truth (``LeaseSpec``) and prints
each fact in exactly one canonical sentence (``CLAUSE_TEMPLATES``), padded by
enough varied boilerplate that the sentence lands on a different page in
every lease. That is what makes a citation checkable: the test knows which
page the answer is on because it put it there.

Three consumers depend on the shapes in this module and are coordinated with
it by tests:

- ``services.extraction_fields`` writes its stub patterns against the
  canonical sentences, so the extractive provider round-trips every value.
- ``demo.seed`` renders the specs into documents and turns the golden
  questions into the evaluation set.
- ``samples/`` holds three of these leases, committed so the upload flow can
  be tried without running the seed.

Everything is deterministic for a seed: the same call gives the same specs,
the same PDF bytes, the same questions.
"""

from __future__ import annotations

import io
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)

DEFAULT_SEED = 2026
DEMO_LEASE_COUNT = 48

#: (name, number of leases, confidential). Riverside is the portfolio being
#: sold; the seed makes it visible to the director and one manager only.
PORTFOLIO_PLAN: list[tuple[str, int, bool]] = [
    ("City Centre", 20, False),
    ("Northern Estates", 18, False),
    ("Riverside", 10, True),
]
_PORTFOLIO_CODES = {"City Centre": "CC", "Northern Estates": "NE", "Riverside": "RS"}

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

REVIEW_BASIS_TEXT = {
    "open_market": "open market",
    "rpi": "RPI",
    "cpi": "CPI",
    "fixed_uplift": "fixed uplift",
    "none": "none",
}
REPAIR_TEXT = {
    "full_repairing": "full repairing",
    "internal_repairing": "internal repairing",
    "landlord_repairing": "landlord repairing",
}

#: One sentence per fact, fixed wording. The registry's stub patterns are
#: written against these; change one and the other must change with it
#: (tests/test_leases.py checks the round trip for every lease).
CLAUSE_TEMPLATES: dict[str, str] = {
    "landlord_name": (
        "The Landlord is {landlord_name}, {landlord_description}, whose registered office "
        "is at {landlord_address}."
    ),
    "tenant_name": (
        "The Tenant is {tenant_name}, {tenant_description}, whose registered office is at "
        "{tenant_address}."
    ),
    "guarantor": "The Guarantor is {guarantor}, {guarantor_description}.",
    "property_address": "The Building is {property_address}.",
    "unit": (
        "The Premises are known as {unit}, {property_name} and are shown edged red on the Plan."
    ),
    "term_years": "The Premises are let for a term of {term_years} years (the Term).",
    "term": "The Term shall commence on {term_start} and shall expire on {term_end}.",
    "annual_rent_gbp": "The Initial Rent is {annual_rent} ({annual_rent_words} pounds) per annum.",
    "rent_review_basis": "The Review Basis is {rent_review_basis}.",
    "rent_review_date": "The Review Date is {rent_review_date}.",
    "break": (
        "The Tenant may determine this Lease on {break_date} by giving the Landlord not less "
        "than {break_notice_months} months' prior written notice."
    ),
    "repairing_obligation": "The repairing basis of this Lease is {repairing_obligation}.",
    "permitted_use": "The Permitted Use is {permitted_use}.",
    "deposit_gbp": (
        "The Deposit is {deposit} ({deposit_words} pounds), which the Tenant shall pay to the "
        "Landlord on the date of this Lease."
    ),
    "vat_elected": (
        "The Landlord has {vat_elected} to waive the exemption from VAT in respect of the Building."
    ),
}

#: Registry field -> the template that prints it. Two facts share a sentence
#: for the term and for the break, because that is how leases say them.
FIELD_TEMPLATE: dict[str, str] = {
    "tenant_name": "tenant_name",
    "landlord_name": "landlord_name",
    "property_address": "property_address",
    "unit": "unit",
    "term_start": "term",
    "term_end": "term",
    "term_years": "term_years",
    "annual_rent_gbp": "annual_rent_gbp",
    "rent_review_basis": "rent_review_basis",
    "rent_review_date": "rent_review_date",
    "break_date": "break",
    "break_notice_months": "break",
    "repairing_obligation": "repairing_obligation",
    "permitted_use": "permitted_use",
    "deposit_gbp": "deposit_gbp",
    "guarantor": "guarantor",
    "vat_elected": "vat_elected",
}


@dataclass(frozen=True)
class Distractor:
    """A fact that looks like a registry value and is not one -- another
    date, another sum, another party -- printed so an extractor has to read
    the sentence rather than grab the nearest number."""

    key: str
    label: str
    sentence: str


@dataclass(frozen=True)
class LeaseSpec:
    reference: str
    portfolio: str
    property_name: str
    property_address: str
    unit: str
    landlord_name: str
    landlord_description: str
    landlord_address: str
    tenant_name: str
    tenant_description: str
    tenant_address: str
    lease_date: date
    term_start: date
    term_end: date
    term_years: int
    annual_rent_gbp: Decimal
    rent_review_basis: str
    rent_review_date: date
    break_date: date | None
    break_notice_months: int | None
    repairing_obligation: str
    permitted_use: str
    deposit_gbp: Decimal
    guarantor: str | None
    guarantor_description: str | None
    vat_elected: bool
    distractors: tuple[Distractor, ...]

    @property
    def title(self) -> str:
        return f"{self.unit}, {self.property_name}"

    @property
    def filename(self) -> str:
        return f"{slugify(self.property_name)}-{slugify(self.unit)}.pdf"

    @property
    def metadata(self) -> dict[str, str]:
        """Hints stored on the document row for the documents screen."""
        return {
            "property": self.property_name,
            "unit": self.unit,
            "tenant": self.tenant_name,
            "reference": self.reference,
        }


@dataclass
class GoldenQ:
    question: str
    answerable: bool
    expected_pages: list[int]
    field_key: str | None


# ------------------------------------------------------------- formatting --


def format_date(value: date) -> str:
    """ "1 April 2024": no zero padding, no ordinal suffix, English month."""
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def format_money(value: Decimal | int) -> str:
    return f"£{int(value):,}"


_ONES = (
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _below_thousand(n: int) -> str:
    parts: list[str] = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
        if n:
            parts.append("and")
    if n >= 20:
        parts.append(f"{_TENS[n // 10]}-{_ONES[n % 10]}" if n % 10 else _TENS[n // 10])
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def money_words(value: Decimal | int) -> str:
    """The sum in words as a lease prints it: "forty-two thousand five hundred"."""
    n = int(value)
    if n == 0:
        return "zero"
    parts: list[str] = []
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1_000)
    if millions:
        parts.append(f"{_below_thousand(millions)} million")
    if thousands:
        parts.append(f"{_below_thousand(thousands)} thousand")
    if units:
        if parts and units < 100:
            parts.append("and")
        parts.append(_below_thousand(units))
    return " ".join(parts)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _add_years(value: date, years: int) -> date:
    # No generated date falls on 29 February, so this never needs to clamp.
    return value.replace(year=value.year + years)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month)


# ------------------------------------------------------------------ pools --


@dataclass(frozen=True)
class _Property:
    name: str
    street: str | None
    town: str
    postcode: str
    kind: str  # office | retail | industrial | mixed
    units: tuple[str, ...]

    @property
    def address(self) -> str:
        parts = [self.name, self.street, f"{self.town} {self.postcode}"]
        return ", ".join(part for part in parts if part)


_PROPERTIES: dict[str, tuple[_Property, ...]] = {
    "City Centre": (
        _Property(
            "Meridian House",
            "12 Mosley Street",
            "Manchester",
            "M2 3AN",
            "office",
            ("Unit 4", "Suite 2B", "Second Floor", "Third Floor", "Fourth Floor"),
        ),
        _Property(
            "Whitworth Court",
            "45 Whitworth Street",
            "Manchester",
            "M1 6EY",
            "office",
            ("Unit 2B", "Unit 3A", "First Floor", "Fifth Floor"),
        ),
        _Property(
            "12 Deansgate",
            None,
            "Manchester",
            "M3 2BW",
            "mixed",
            ("Unit 1", "Unit 2", "First Floor", "Second Floor"),
        ),
        _Property(
            "St Ann's Chambers",
            "6 St Ann's Square",
            "Manchester",
            "M2 7HL",
            "office",
            ("Suite 1", "Suite 3", "Suite 5", "Third Floor"),
        ),
        _Property(
            "Spring Gardens House",
            "28 Spring Gardens",
            "Manchester",
            "M2 1AB",
            "office",
            ("Ground Floor", "First Floor", "Second Floor", "Suite 4A"),
        ),
        _Property(
            "King Street Exchange",
            "82 King Street",
            "Manchester",
            "M2 4WQ",
            "mixed",
            ("Unit 1", "Unit 3", "Unit 5", "Second Floor"),
        ),
        _Property(
            "Piccadilly Gate House",
            "6 Piccadilly",
            "Manchester",
            "M1 3BN",
            "office",
            ("Suite 2", "Suite 6", "Fourth Floor", "Sixth Floor"),
        ),
        _Property(
            "Portland Chambers",
            "53 Portland Street",
            "Manchester",
            "M1 3LD",
            "office",
            ("Unit 2", "First Floor", "Third Floor"),
        ),
        _Property(
            "Lever Street Studios",
            "19 Lever Street",
            "Manchester",
            "M1 1BY",
            "office",
            ("Studio 3", "Studio 7", "Studio 12"),
        ),
        _Property(
            "Cross Street Arcade",
            "31 Cross Street",
            "Manchester",
            "M2 1WL",
            "retail",
            ("Unit 2", "Unit 4", "Unit 9"),
        ),
    ),
    "Northern Estates": (
        _Property(
            "Parkway Trading Estate",
            "Trafford Park",
            "Manchester",
            "M17 1HW",
            "industrial",
            ("Unit 1", "Unit 5", "Unit 7", "Unit 12", "Unit 14"),
        ),
        _Property(
            "Barton Dock Business Park",
            "Barton Dock Road, Trafford Park",
            "Manchester",
            "M41 7ZA",
            "industrial",
            ("Unit 3", "Unit 8", "Unit 10", "Unit 16"),
        ),
        _Property(
            "Stamford Quarter",
            "George Street",
            "Altrincham",
            "WA14 1RH",
            "retail",
            ("Unit 2", "Unit 4", "Unit 6", "Unit 11"),
        ),
        _Property(
            "Grafton Mall",
            "Grafton Street",
            "Altrincham",
            "WA14 1DU",
            "retail",
            ("Unit 3", "Unit 5", "Unit 8"),
        ),
        _Property(
            "Lancastrian Business Park",
            "Bark Street",
            "Bolton",
            "BL1 2AX",
            "office",
            ("Unit 2", "Unit 6", "Suite 3"),
        ),
        _Property(
            "Rock Retail Terrace",
            "The Rock",
            "Bury",
            "BL9 0JD",
            "retail",
            ("Unit 1", "Unit 4", "Unit 7"),
        ),
        _Property(
            "Heywood Distribution Park",
            "Pilsworth Road",
            "Heywood",
            "OL10 2TT",
            "industrial",
            ("Unit A2", "Unit B1", "Unit C4"),
        ),
        _Property(
            "Chadderton Business Centre",
            "Broadway, Chadderton",
            "Oldham",
            "OL9 9XA",
            "mixed",
            ("Unit 5", "Unit 9", "Suite 2"),
        ),
        _Property(
            "Merseyway Parade",
            "Merseyway",
            "Stockport",
            "SK1 1PD",
            "retail",
            ("Unit 3", "Unit 6", "Unit 10"),
        ),
        _Property(
            "Martland Mill Estate",
            "Martland Mill Lane",
            "Wigan",
            "WN5 0LX",
            "industrial",
            ("Unit 2", "Unit 4", "Unit 11"),
        ),
    ),
    "Riverside": (
        _Property(
            "Deansgate Quays",
            "3 Water Street",
            "Manchester",
            "M3 4JU",
            "office",
            ("Suite 3", "Suite 5", "Third Floor", "Fifth Floor"),
        ),
        _Property(
            "Anchorage Wharf",
            "Anchorage Quay, Salford Quays",
            "Salford",
            "M50 3XW",
            "office",
            ("Unit 2", "Unit 6", "Second Floor"),
        ),
        _Property(
            "Clippers Quay House",
            "Clippers Quay, Salford Quays",
            "Salford",
            "M50 3XP",
            "office",
            ("Suite 1", "Suite 4", "Fourth Floor"),
        ),
        _Property(
            "Irwell Bank House",
            "Chapel Wharf",
            "Salford",
            "M3 5JZ",
            "mixed",
            ("Unit 1", "Unit 3", "Ground Floor"),
        ),
        _Property(
            "Castlefield Locks",
            "Castle Street",
            "Manchester",
            "M15 4LZ",
            "mixed",
            ("Unit 2", "Unit 5", "Arch 7"),
        ),
    ),
}

#: (name, company number, registered office)
_LANDLORDS: tuple[tuple[str, str, str], ...] = (
    ("Bridgewater Estates Limited", "03318842", "3 Hardman Street, Manchester M3 3HF"),
    (
        "Pennine Property Holdings Limited",
        "04102377",
        "Pennine House, Bradford Road, Leeds LS1 4BR",
    ),
    ("Irwell Investments Limited", "05587120", "14 Chapel Street, Salford M3 7NH"),
    ("Mancunian Freehold Investments Limited", "02291508", "77 Fountain Street, Manchester M2 2EE"),
    ("Ardwick Property Company Limited", "06044913", "Ardwick Green North, Manchester M12 6FZ"),
    ("Rochdale Canal Estates Limited", "01978344", "9 Dale Street, Manchester M1 1JA"),
    ("Castlefield Land Limited", "07713265", "2 Liverpool Road, Manchester M3 4FP"),
    ("Northern Counties Property Trust Limited", "03720481", "1 Park Row, Leeds LS1 5AB"),
    ("Salford Riverside Developments Limited", "08246137", "The Quays, Salford M50 3AZ"),
    (
        "Ashton Estates (Holdings) Limited",
        "02883604",
        "Warrington Street, Ashton-under-Lyne OL6 6XB",
    ),
    ("Cheshire and Northern Land Limited", "05137792", "Lloyd Street, Altrincham WA14 2DE"),
    ("Trafford Wharfside Property Limited", "06612088", "Wharfside Way, Manchester M17 1GY"),
    ("Peak District Property Investments LLP", "OC371402", "Market Place, Stockport SK1 1EU"),
    ("Booth Street Property Partners LLP", "OC398115", "8 Booth Street, Manchester M2 4AW"),
)

_PLACES = (
    "Chorlton",
    "Didsbury",
    "Sale",
    "Altrincham",
    "Stretford",
    "Urmston",
    "Eccles",
    "Swinton",
    "Prestwich",
    "Bury",
    "Bolton",
    "Rochdale",
    "Oldham",
    "Ashton",
    "Stockport",
    "Marple",
    "Wilmslow",
    "Knutsford",
    "Northwich",
    "Warrington",
    "Wigan",
    "Leigh",
    "Radcliffe",
    "Whitefield",
    "Middleton",
    "Heywood",
    "Denton",
    "Hyde",
    "Cheadle",
    "Bramhall",
    "Hale",
    "Timperley",
    "Levenshulme",
    "Withington",
    "Fallowfield",
    "Hulme",
    "Ancoats",
    "Salford",
    "Monton",
    "Worsley",
    "Irlam",
    "Glossop",
)

_USES = {
    "office": "offices within Class E(g)(i) of the Use Classes Order",
    "retail": "the retail sale of goods within Class E(a) of the Use Classes Order",
    "cafe": "a cafe within Class E(b) of the Use Classes Order",
    "clinic": (
        "the provision of medical or health services within Class E(e) of the Use Classes Order"
    ),
    "gym": "indoor sport and fitness within Class E(d) of the Use Classes Order",
    "light_industrial": "light industrial purposes within Class E(g)(iii) of the Use Classes Order",
    "storage": "storage and distribution within Class B8 of the Use Classes Order",
    "workshop": "general industrial purposes within Class B2 of the Use Classes Order",
    "studio": (
        "a creative studio with ancillary offices within Class E(g)(ii) of the Use Classes Order"
    ),
}

#: (trade, use key, entity forms it may take). Trades are matched to the kind
#: of building so a fastenings firm does not end up on the third floor of a
#: city-centre office block.
_TRADES: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "office": (
        ("Accounting", "office", ("Limited", "LLP")),
        ("Solicitors", "office", ("LLP",)),
        ("Software", "office", ("Limited",)),
        ("Recruitment", "office", ("Limited",)),
        ("Marketing", "office", ("Limited",)),
        ("Financial Planning", "office", ("Limited",)),
        ("Architects", "office", ("LLP",)),
        ("Consulting", "office", ("Limited", "LLP")),
        ("Insurance Brokers", "office", ("Limited",)),
        ("Estate Agents", "office", ("Limited",)),
        ("Data Services", "office", ("Limited", "plc")),
        ("Surveyors", "office", ("LLP",)),
        ("Creative Studios", "studio", ("Limited",)),
        ("Media", "studio", ("Limited",)),
        ("Learning Centre", "office", ("Limited",)),
        ("Home Care", "office", ("Limited",)),
    ),
    "retail": (
        ("Bakery", "retail", ("Limited",)),
        ("Opticians", "retail", ("Limited",)),
        ("Book Shop", "retail", ("Limited",)),
        ("Coffee Company", "cafe", ("Limited",)),
        ("Pharmacy", "retail", ("Limited", "plc")),
        ("Kitchens", "retail", ("Limited",)),
        ("Interiors", "retail", ("Limited",)),
        ("Cycles", "retail", ("Limited",)),
        ("Physiotherapy", "clinic", ("Limited",)),
        ("Dental Practice", "clinic", ("Limited",)),
        ("Veterinary Group", "clinic", ("Limited",)),
        ("Fitness", "gym", ("Limited",)),
    ),
    "industrial": (
        ("Fastenings", "light_industrial", ("Limited",)),
        ("Logistics", "storage", ("Limited", "plc")),
        ("Packaging", "storage", ("Limited", "plc")),
        ("Vehicle Parts", "storage", ("Limited",)),
        ("Electrical Wholesale", "storage", ("Limited",)),
        ("Timber Supplies", "storage", ("Limited",)),
        ("Screen Printing", "light_industrial", ("Limited",)),
        ("Textiles", "light_industrial", ("Limited",)),
        ("Storage Solutions", "storage", ("Limited",)),
        ("Engineering", "workshop", ("Limited", "plc")),
        ("Medical Supplies", "storage", ("Limited",)),
        ("Brewing Company", "workshop", ("Limited",)),
    ),
}
_TRADES["mixed"] = _TRADES["office"][:8] + _TRADES["retail"][:8]

_REGISTERED_OFFICES = (
    "Unit 9, Wellington Mill, Wellington Road, Stockport SK4 1AB",
    "2 Booth Street West, Manchester M15 6PD",
    "Suite 12, Ashley House, Ashley Road, Altrincham WA14 2DW",
    "The Old Bank, 3 Market Street, Bury BL9 0AJ",
    "41 Bridge Street, Manchester M3 3BZ",
    "Regent House, Regent Road, Salford M5 4QA",
    "17 Cross Street, Sale M33 7FT",
    "Bank Chambers, 4 Deansgate, Bolton BL1 1DA",
    "6 Cathedral Yard, Manchester M3 1SG",
    "Lakeside House, Cheadle Royal, Cheadle SK8 3GX",
    "Riverside Works, Wood Street, Rochdale OL16 1XY",
    "22 Lever Street, Manchester M1 1DZ",
    "Stanley Court, Ridgefield, Manchester M2 6EQ",
    "8 Church Street, Wilmslow SK9 1AX",
    "Victoria Mill, Lower Vickers Street, Manchester M40 7LH",
    "3 The Downs, Altrincham WA14 2QD",
)

_PEOPLE = (
    "Mr Andrew Kershaw",
    "Mrs Helen Ogunyemi",
    "Ms Farah Siddiqui",
    "Mr David Lomax",
    "Mrs Joanne Whitaker",
    "Mr Imran Patel",
    "Ms Rachel Tomlinson",
    "Mr Stephen Aldridge",
    "Mrs Bernadette Quinn",
    "Mr Kwame Boateng",
    "Ms Eleanor Haworth",
    "Mr Piotr Nowak",
)
_HOME_ADDRESSES = (
    "14 Ashworth Road, Altrincham WA14 2XY",
    "7 Beech Avenue, Didsbury, Manchester M20 5BL",
    "28 Moorfield Drive, Bramhall SK7 2LR",
    "3 Larch Close, Worsley M28 2PJ",
    "51 Park Lane, Whitefield M45 7PW",
    "19 Hawthorn Grove, Heaton Moor SK4 4EL",
    "9 Clifton Road, Prestwich M25 3HQ",
    "36 Woodlands Road, Hale WA15 8DZ",
)

_RENT_RANGES = {
    "office": (18_000, 160_000),
    "retail": (12_000, 85_000),
    "industrial": (22_000, 240_000),
    "mixed": (15_000, 120_000),
}
#: A multi-let office block is usually internal repairing with a service
#: charge; an industrial unit is usually full repairing; a landlord who
#: repairs is rare and mostly found on small retail and mixed-use lettings.
_REPAIR_WEIGHTS = {
    "office": (("internal_repairing", 72), ("full_repairing", 20), ("landlord_repairing", 8)),
    "retail": (("internal_repairing", 45), ("full_repairing", 30), ("landlord_repairing", 25)),
    "industrial": (("full_repairing", 85), ("internal_repairing", 15)),
    "mixed": (("internal_repairing", 55), ("full_repairing", 30), ("landlord_repairing", 15)),
}
_QUARTER_DAYS = ((3, 25), (6, 24), (9, 29), (12, 25))


# ------------------------------------------------------------- generation --


def _portfolio_sequence(n: int) -> list[str]:
    """Portfolio for each of ``n`` leases, in the plan's proportions.

    Sequential largest-remainder allocation: at each step the portfolio
    furthest behind its share gets the next lease, so any prefix of the
    sequence is as balanced as it can be and 48 comes out exactly 20/18/10.
    """
    total = sum(count for _, count, _ in PORTFOLIO_PLAN)
    assigned = dict.fromkeys((name for name, _, _ in PORTFOLIO_PLAN), 0)
    sequence: list[str] = []
    for i in range(1, n + 1):
        name = max(
            PORTFOLIO_PLAN,
            key=lambda plan: (plan[1] * i / total - assigned[plan[0]], plan[1]),
        )[0]
        assigned[name] += 1
        sequence.append(name)
    return sequence


def _entity_description(name: str, number: str) -> str:
    if name.endswith("LLP"):
        return (
            f"a limited liability partnership registered in England and Wales with number {number}"
        )
    return f"a company registered in England and Wales with company number {number}"


def _company_number(rng: random.Random, name: str) -> str:
    if name.endswith("LLP"):
        return f"OC{rng.randint(300_000, 449_999)}"
    return f"{rng.randint(1_900_000, 13_999_999):08d}"


def _weighted(rng: random.Random, options: tuple[tuple[str, int], ...]) -> str:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options], k=1)[0]


def _pick_start(rng: random.Random) -> date:
    year = rng.randint(2016, 2025)
    if rng.random() < 0.3:
        month, day = rng.choice(_QUARTER_DAYS)
        return date(year, month, day)
    return date(year, rng.randint(1, 12), 1)


def _pick_tenant(rng: random.Random, kind: str, used: set[str]) -> tuple[str, str]:
    """A tenant name unique across the run, and its permitted use key."""
    for _ in range(200):
        trade, use_key, forms = rng.choice(_TRADES[kind])
        name = f"{rng.choice(_PLACES)} {trade} {rng.choice(forms)}"
        if name not in used:
            used.add(name)
            return name, use_key
    raise ValueError("Ran out of distinct tenant names; enlarge _PLACES or _TRADES.")


def _guarantor(rng: random.Random, tenant_name: str) -> tuple[str | None, str | None]:
    """Who stands behind the tenant. A plc stands behind itself; a smaller
    company gives a parent or a director."""
    if tenant_name.endswith("plc") or rng.random() < 0.25:
        return None, None
    if rng.random() < 0.55:
        # The parent takes the place name: "Chorlton Bakery Limited" is owned
        # by "Chorlton Holdings Limited", which is how such groups are named.
        name = f"{tenant_name.split(' ', 1)[0]} {rng.choice(('Holdings', 'Group'))} Limited"
        return name, _entity_description(name, _company_number(rng, name))
    return rng.choice(_PEOPLE), f"of {rng.choice(_HOME_ADDRESSES)}"


def _distractors(rng: random.Random, term_start: date, rent: int) -> tuple[Distractor, ...]:
    candidates = [
        Distractor(
            "rent_commencement_date",
            "Rent commencement date",
            "The Rent Commencement Date is "
            f"{format_date(_add_months(term_start, rng.choice((3, 6))))}.",
        ),
        Distractor(
            "service_charge_cap",
            "Service charge cap",
            "The Service Charge payable by the Tenant for the first Service Charge Year shall not "
            f"exceed {format_money(rent * 12 // 100 // 50 * 50)}.",
        ),
        Distractor(
            "insurance_rent_estimate",
            "Insurance rent estimate",
            "The Insurance Rent for the first year of the Term is estimated at "
            f"{format_money(rent * 3 // 100 // 10 * 10)} and is payable within fourteen days of "
            "demand.",
        ),
        Distractor(
            "managing_agent",
            "Managing agent",
            "The Landlord's managing agent is Hallam & Pryce of 8 Booth Street, Manchester "
            "M2 4AW, to whom rent demands and notices on behalf of the Landlord may be "
            "addressed.",
        ),
        Distractor(
            "previous_rent",
            "Rent under the previous lease",
            "For the avoidance of doubt, the rent reserved by the previous lease of the Premises, "
            f"being {format_money(rent * rng.randint(78, 94) // 100 // 250 * 250)} per annum, "
            "is superseded by this Lease.",
        ),
    ]
    count = rng.choice((2, 3, 3))
    chosen = rng.sample(candidates, count)
    # Keep the pool's order so the same set renders in the same places.
    return tuple(d for d in candidates if d in chosen)


def generate_specs(n: int, seed: int = DEFAULT_SEED) -> list[LeaseSpec]:
    """``n`` leases, the same ones every time for a seed.

    Every draw comes from one ``random.Random`` in a fixed order, so adding a
    field means adding a draw at the end of the loop, not in the middle, if
    existing seeds are to keep their leases.
    """
    rng = random.Random(seed)
    pools: dict[str, list[tuple[_Property, str]]] = {}
    for portfolio, properties in _PROPERTIES.items():
        pairs = [(prop, unit) for prop in properties for unit in prop.units]
        rng.shuffle(pairs)
        pools[portfolio] = pairs

    used_tenants: set[str] = set()
    counters = dict.fromkeys(_PORTFOLIO_CODES, 0)
    specs: list[LeaseSpec] = []
    for portfolio in _portfolio_sequence(n):
        if not pools[portfolio]:
            raise ValueError(f"The {portfolio} portfolio has no units left to let; n is too large.")
        prop, unit = pools[portfolio].pop()
        counters[portfolio] += 1
        reference = f"HP/{_PORTFOLIO_CODES[portfolio]}/{counters[portfolio]:04d}"

        landlord_name, landlord_number, landlord_address = rng.choice(_LANDLORDS)
        tenant_name, use_key = _pick_tenant(rng, prop.kind, used_tenants)
        tenant_number = _company_number(rng, tenant_name)
        tenant_address = rng.choice(_REGISTERED_OFFICES)

        term_years = _weighted(rng, (("5", 30), ("10", 45), ("15", 18), ("20", 7)))
        years = int(term_years)
        term_start = _pick_start(rng)
        term_end = _add_years(term_start, years) - timedelta(days=1)
        lease_date = term_start - timedelta(days=rng.randint(7, 45))

        low, high = _RENT_RANGES[prop.kind]
        rent = rng.randint(low // 250, high // 250) * 250
        basis = _weighted(
            rng, (("open_market", 45), ("rpi", 25), ("cpi", 10), ("fixed_uplift", 20))
        )
        review_interval = 3 if years <= 5 else 5
        review_date = _add_years(term_start, review_interval)

        break_date: date | None = None
        break_notice: int | None = None
        if rng.random() < 0.55:
            break_year = {5: 3, 10: 5, 15: rng.choice((5, 10)), 20: 10}[years]
            break_date = _add_years(term_start, break_year) - timedelta(days=1)
            break_notice = rng.choice((3, 6, 6, 6, 9, 12))

        repair = _weighted(rng, _REPAIR_WEIGHTS[prop.kind])
        deposit_months = rng.choice((3, 3, 6))
        deposit = rent * deposit_months // 12 // 50 * 50
        guarantor, guarantor_description = _guarantor(rng, tenant_name)
        vat_elected = rng.random() < 0.65
        distractors = _distractors(rng, term_start, rent)

        specs.append(
            LeaseSpec(
                reference=reference,
                portfolio=portfolio,
                property_name=prop.name,
                property_address=prop.address,
                unit=unit,
                landlord_name=landlord_name,
                landlord_description=_entity_description(landlord_name, landlord_number),
                landlord_address=landlord_address,
                tenant_name=tenant_name,
                tenant_description=_entity_description(tenant_name, tenant_number),
                tenant_address=tenant_address,
                lease_date=lease_date,
                term_start=term_start,
                term_end=term_end,
                term_years=years,
                annual_rent_gbp=Decimal(rent),
                rent_review_basis=basis,
                rent_review_date=review_date,
                break_date=break_date,
                break_notice_months=break_notice,
                repairing_obligation=repair,
                permitted_use=_USES[use_key],
                deposit_gbp=Decimal(deposit),
                guarantor=guarantor,
                guarantor_description=guarantor_description,
                vat_elected=vat_elected,
                distractors=distractors,
            )
        )
    return specs


def assign_portfolios(specs: list[LeaseSpec]) -> list[tuple[str, LeaseSpec]]:
    """(portfolio name, spec) pairs. The portfolio is decided at generation,
    because which building a lease is in decides which portfolio it belongs
    to; this is the seed's view of that decision."""
    return [(spec.portfolio, spec) for spec in specs]


# ---------------------------------------------------------------- facts --


def fact_sentences(spec: LeaseSpec) -> dict[str, str]:
    """Every canonical sentence the lease prints, keyed by template name.
    Facts the lease does not have (no break, no guarantor) are absent."""
    values = {
        "landlord_name": spec.landlord_name,
        "landlord_description": spec.landlord_description,
        "landlord_address": spec.landlord_address,
        "tenant_name": spec.tenant_name,
        "tenant_description": spec.tenant_description,
        "tenant_address": spec.tenant_address,
        "property_address": spec.property_address,
        "property_name": spec.property_name,
        "unit": spec.unit,
        "term_years": spec.term_years,
        "term_start": format_date(spec.term_start),
        "term_end": format_date(spec.term_end),
        "annual_rent": format_money(spec.annual_rent_gbp),
        "annual_rent_words": money_words(spec.annual_rent_gbp),
        "rent_review_basis": REVIEW_BASIS_TEXT[spec.rent_review_basis],
        "rent_review_date": format_date(spec.rent_review_date),
        "repairing_obligation": REPAIR_TEXT[spec.repairing_obligation],
        "permitted_use": spec.permitted_use,
        "deposit": format_money(spec.deposit_gbp),
        "deposit_words": money_words(spec.deposit_gbp),
        "vat_elected": "elected" if spec.vat_elected else "not elected",
    }
    sentences = {
        key: template.format(**values)
        for key, template in CLAUSE_TEMPLATES.items()
        if key not in ("break", "guarantor")
    }
    if spec.break_date is not None and spec.break_notice_months is not None:
        sentences["break"] = CLAUSE_TEMPLATES["break"].format(
            break_date=format_date(spec.break_date),
            break_notice_months=spec.break_notice_months,
        )
    if spec.guarantor is not None:
        sentences["guarantor"] = CLAUSE_TEMPLATES["guarantor"].format(
            guarantor=spec.guarantor, guarantor_description=spec.guarantor_description
        )
    return sentences


def canonical_sentence(spec: LeaseSpec, field_key: str) -> str | None:
    """The sentence that states ``field_key``, or None when the lease has no
    such fact."""
    return fact_sentences(spec).get(FIELD_TEMPLATE[field_key])


def present_fields(spec: LeaseSpec) -> list[str]:
    """Registry keys the lease actually states, in registry order."""
    return [key for key in FIELD_TEMPLATE if canonical_sentence(spec, key) is not None]


# ----------------------------------------------------------- boilerplate --

_DEFINITIONS = (
    '"1954 Act" means the Landlord and Tenant Act 1954.',
    '"Building" means the building described in clause 3 (Demise and Term), including all '
    "additions and alterations to it and the land on which it stands.",
    '"Common Parts" means the entrance halls, corridors, staircases, lifts, landings, '
    "toilets, car parks, service roads, landscaped areas and other parts of the Building which "
    "are provided by the Landlord for the common use of the tenants and occupiers of the "
    "Building.",
    '"Conduits" means the pipes, wires, cables, sewers, drains, ducts, flues, gutters, '
    "channels and other conducting media of any kind serving the Premises or the Building.",
    '"Encumbrances" means the matters affecting the Premises which are registered against '
    "the Landlord's title at the date of this Lease, so far as they are still subsisting and "
    "capable of taking effect.",
    '"Group Company" means a company which is a member of the same group as the Tenant '
    "within the meaning of section 42 of the 1954 Act.",
    '"Insured Risks" means fire, lightning, explosion, storm, flood, earthquake, subsidence, '
    "riot, civil commotion, malicious damage, impact by vehicles or aircraft, bursting or "
    "overflowing of water tanks and pipes, and such other risks as the Landlord may from time "
    "to time reasonably decide to insure against.",
    '"Insurance Rent" means the sums which the Landlord spends on insuring the Building '
    "against the Insured Risks and against loss of Rent for three years, together with the "
    "cost of any valuation for insurance purposes.",
    '"Interest" means interest at the rate of four per cent per annum above the base rate '
    "from time to time of the Bank of England, calculated on a daily basis from the date on "
    "which a sum falls due until the date of payment, both before and after any judgment.",
    '"Landlord\'s Surveyor" means any person appointed by or acting for the Landlord to '
    "perform the function of a surveyor for any purpose of this Lease.",
    '"Permitted Use" means the use permitted by clause 7 (Use) and no other use.',
    '"Plan" means the plan annexed to this Lease.',
    '"Planning Acts" means the Town and Country Planning Act 1990 and every other statute '
    "for the time being in force relating to town and country planning.",
    '"Premises" means the premises described in clause 3 (Demise and Term), including the '
    "Landlord's fixtures and fittings in them and all additions and improvements made to them.",
    '"Quarter Days" means 25 March, 24 June, 29 September and 25 December in every year.',
    '"Rent" means the Initial Rent as reviewed in accordance with clause 5 (Rent Review).',
    '"Review Date" means the date specified as such in clause 5 (Rent Review).',
    '"Service Charge" means the fair proportion, determined by the Landlord\'s Surveyor, of '
    "the costs incurred by the Landlord in providing the services described in Schedule 3.",
    '"Service Charge Year" means the period of twelve months ending on 31 December in every '
    "year, or such other period as the Landlord may from time to time notify to the Tenant.",
    '"Term" means the term of years granted by clause 3 (Demise and Term), together with any '
    "continuation of that term by statute or at common law.",
    '"Term Commencement Date" means the date on which the Term commences under clause 3 '
    "(Demise and Term).",
    '"Uninsured Risks" means any of the Insured Risks against which insurance is not '
    "available in the London insurance market on reasonable terms.",
    '"Use Classes Order" means the Town and Country Planning (Use Classes) Order 1987 as it '
    "applies at the date of this Lease.",
    '"Utilities" means water, gas, electricity, telecommunications and any other services '
    "supplied to the Premises.",
    '"VAT" means value added tax chargeable under the Value Added Tax Act 1994 and any '
    "similar or replacement tax.",
    '"Working Day" means any day other than a Saturday, Sunday or public holiday in England.',
)
#: Definitions every lease needs because a clause refers to them.
_CORE_DEFINITIONS = frozenset({1, 10, 11, 13, 15, 16, 19, 20, 22, 24})

_INTERPRETATION = (
    "Any reference to a statute includes any subordinate legislation made under it and any "
    "statutory modification or re-enactment of it for the time being in force.",
    "The clause headings do not affect the interpretation of this Lease.",
    "Words importing one gender include every gender, and words importing the singular "
    "include the plural and vice versa.",
    "Any obligation on the Tenant not to do something includes an obligation not to permit or "
    "suffer it to be done by any other person.",
    "Where the consent or approval of the Landlord is required under this Lease, it must be "
    "obtained in writing before the act concerned is carried out.",
    "Any reference to the Landlord includes the person for the time being entitled to the "
    "reversion immediately expectant on the end of the Term.",
    "If any provision of this Lease is held to be invalid or unenforceable, the remaining "
    "provisions shall continue in full force and effect.",
    "References to the end of the Term include its determination by forfeiture, surrender, "
    "the exercise of any break right or otherwise.",
)

_PARTIES = (
    "Each party is referred to in this Lease by the description given above, and each such "
    "description includes that party's successors in title and assigns and, in the case of an "
    "individual, personal representatives.",
    "Where a party comprises more than one person, the obligations of that party are joint and "
    "several obligations of each of those persons.",
    "This Lease is executed as a deed and the parties intend it to take effect as such on the "
    "date stated above, whether or not it is dated by hand on that date.",
    "The Landlord and the Tenant confirm that they have each taken independent legal advice on "
    "the terms of this Lease before executing it.",
)

_DEMISE = (
    "The Landlord lets the Premises to the Tenant with full title guarantee, together with the "
    "rights set out in Schedule 1, excepting and reserving to the Landlord the rights set out "
    "in Schedule 2, and subject to the Encumbrances.",
    "The Tenant shall not be entitled to any rights of light or air or other easements over any "
    "adjoining property of the Landlord except those expressly granted by this Lease.",
    "The Tenant acknowledges that the Premises are let in their present condition and that the "
    "Landlord has made no representation as to their fitness for the Permitted Use.",
    "The Landlord reserves the right to alter the Common Parts, to erect or alter buildings on "
    "adjoining land, and to grant rights over the Building to third parties, provided that the "
    "Tenant's use of the Premises is not materially prejudiced.",
    "The demise excludes the roof, the foundations, the structural walls and the Conduits of "
    "the Building which serve other premises, but includes the internal non-structural walls, "
    "the floor finishes, the ceilings and the internal surfaces of the structural walls.",
    "The Tenant shall permit the Landlord and the Landlord's Surveyor to enter the Premises on "
    "reasonable notice to inspect their condition, to take measurements, and to carry out any "
    "works which the Landlord is entitled to carry out under this Lease.",
    "Nothing in this Lease shall operate to grant the Tenant any rights over the airspace above "
    "the Building or the ground beneath its foundations.",
    "The Tenant is granted the right, in common with the Landlord and all others authorised by "
    "the Landlord, to use the Common Parts for the purposes of access to and egress from the "
    "Premises.",
    "The parties agree that sections 24 to 28 of the 1954 Act shall not apply to the tenancy "
    "created by this Lease, the Landlord having served notice on the Tenant and the Tenant "
    "having made a statutory declaration before the parties became contractually bound.",
    "The Landlord confirms that it has the power to grant this Lease and that the Premises are "
    "not subject to any restriction which would prevent their use for the Permitted Use.",
    "If any part of the Premises is destroyed or damaged so as to be unfit for occupation and "
    "use, the Rent shall be suspended in accordance with clause 9 (Insurance) until the "
    "Premises have been reinstated.",
    "The Tenant shall not obstruct the Common Parts, nor leave any goods, vehicles or refuse in "
    "them, nor use them for any purpose other than access.",
    "The Term Commencement Date shall be the date from which the Term is calculated for every "
    "purpose of this Lease, whether or not the Tenant has taken occupation by that date.",
)

_RENT = (
    "The Tenant shall pay the Rent by equal quarterly payments in advance on the Quarter Days, "
    "the first payment being a proportionate sum for the period from the date on which the "
    "Rent first becomes payable to the next Quarter Day.",
    "The Rent and all other sums payable under this Lease shall be paid without any deduction, "
    "counterclaim or set-off, whether legal or equitable, save as required by law.",
    "The Tenant shall pay the Rent by banker's standing order or by such other method as the "
    "Landlord may reasonably require from time to time.",
    "If any Rent or other sum is not paid within fourteen days after the date on which it falls "
    "due, the Tenant shall pay Interest on it from the due date until the date of actual "
    "payment.",
    "The Tenant shall also pay to the Landlord as rent the Service Charge, the Insurance Rent "
    "and any VAT chargeable on them, on the dates and in the manner set out in this Lease.",
    "The Tenant shall pay all rates, taxes, assessments and outgoings of every kind which are "
    "charged or assessed on the Premises or on the owner or occupier of them, other than any "
    "tax payable by the Landlord on the receipt of the Rent or on any dealing with the "
    "reversion.",
    "The Tenant shall pay for all Utilities consumed at the Premises, including any standing "
    "charges, meter rents and connection charges, and shall comply with the requirements of "
    "the supplying authorities.",
    "Where any Utility is supplied to the Premises through a meter serving other premises as "
    "well, the Tenant shall pay a fair proportion of the cost as determined by the Landlord's "
    "Surveyor.",
    "The Landlord may, at its discretion and on giving not less than one month's notice to the "
    "Tenant, vary the dates on which the Rent is payable, provided that the Rent remains "
    "payable in advance.",
    "All sums payable by the Tenant under this Lease are exclusive of VAT, which the Tenant "
    "shall pay in addition where it is chargeable, against receipt of a valid VAT invoice.",
    "The Tenant shall pay the Landlord's proper and reasonable costs of preparing and serving "
    "any schedule of dilapidations at any time during or within three months after the end of "
    "the Term.",
    "Any sum which is payable by the Tenant to the Landlord under this Lease and which is not "
    "otherwise expressed to be rent shall nevertheless be recoverable by the Landlord as rent "
    "in arrear.",
    "The Landlord shall provide the Tenant with a written demand for each payment of Rent not "
    "less than fourteen days before the date on which it falls due, but a failure to do so "
    "shall not relieve the Tenant of the obligation to pay on that date.",
    "The Tenant shall pay a fair proportion, determined by the Landlord's Surveyor, of the cost "
    "of maintaining, repairing and renewing any party walls, fences, roads, Conduits and other "
    "things used in common with other premises.",
)

_REVIEW = (
    "The Rent shall be reviewed on the Review Date in accordance with this clause, and from the "
    "Review Date the Rent shall be the amount determined under this clause.",
    "The Rent shall not be reduced on review, and if the reviewed Rent would be less than the "
    "Rent payable immediately before the Review Date the Rent shall remain unchanged.",
    "If the reviewed Rent has not been agreed or determined by the Review Date, the Tenant "
    "shall continue to pay Rent at the rate payable immediately before the Review Date, and "
    "within fourteen days after the reviewed Rent is agreed or determined the Tenant shall pay "
    "the shortfall together with interest at the base rate from time to time of the Bank of "
    "England.",
    "Time shall not be of the essence for any of the steps in the review procedure, and neither "
    "party shall be prejudiced by any delay in initiating or pursuing the review.",
    "A memorandum recording the reviewed Rent shall be signed by or on behalf of the Landlord "
    "and the Tenant and annexed to this Lease and its counterpart, each party bearing its own "
    "costs.",
    "Any dispute about the reviewed Rent shall be referred to an independent surveyor, to be "
    "agreed between the parties or, failing agreement, to be appointed on the application of "
    "either party by the President of the Royal Institution of Chartered Surveyors.",
    "The independent surveyor shall act as an expert and not as an arbitrator, shall give both "
    "parties the opportunity to make written representations, and shall give reasons for the "
    "determination.",
    "The costs of the independent surveyor shall be borne as the surveyor directs or, in the "
    "absence of a direction, equally between the parties.",
    "If the Review Date falls at a time when the Rent is suspended under clause 9 (Insurance), "
    "the review shall nevertheless proceed on the assumption that the Premises are fit for "
    "occupation and use.",
)
_REVIEW_BY_BASIS = {
    "open_market": (
        "On the Review Date the Rent shall be reviewed to the greater of the Rent payable "
        "immediately before the Review Date and the open market rent of the Premises.",
        "The open market rent is the best annual rent at which the Premises might reasonably be "
        "expected to be let in the open market on the Review Date, with vacant possession, by a "
        "willing landlord to a willing tenant, for a term equal to the unexpired residue of the "
        "Term, on the terms of this Lease other than the amount of the Rent.",
        "The review shall disregard any effect on rent of the Tenant's occupation, any goodwill "
        "attached to the Premises by reason of the Tenant's business, and any improvements "
        "carried out by the Tenant with the Landlord's consent otherwise than in pursuance of "
        "an obligation under this Lease.",
        "The review shall assume that the Premises are in good repair and condition, that the "
        "Tenant has complied with its obligations, and that the Premises may lawfully be used "
        "for the Permitted Use.",
    ),
    "rpi": (
        "On the Review Date the Rent shall be increased in the same proportion as the Retail "
        "Prices Index published by the Office for National Statistics has increased between the "
        "month preceding the Term Commencement Date and the month preceding the Review Date.",
        "If the Retail Prices Index is rebased, the calculation shall be made using the "
        "appropriate conversion factor published by the Office for National Statistics, and if "
        "it ceases to be published the parties shall agree a suitable replacement index.",
        "The reviewed Rent shall be rounded up to the nearest whole pound.",
    ),
    "cpi": (
        "On the Review Date the Rent shall be increased in the same proportion as the Consumer "
        "Prices Index published by the Office for National Statistics has increased between the "
        "month preceding the Term Commencement Date and the month preceding the Review Date.",
        "If the Consumer Prices Index is rebased, the calculation shall be made using the "
        "appropriate conversion factor published by the Office for National Statistics, and if "
        "it ceases to be published the parties shall agree a suitable replacement index.",
        "The reviewed Rent shall be rounded up to the nearest whole pound.",
    ),
    "fixed_uplift": (
        "On the Review Date the Rent shall be increased by ten per cent of the Rent payable "
        "immediately before the Review Date, without any negotiation, valuation or reference to "
        "market conditions.",
        "The parties acknowledge that the fixed uplift has been agreed as a matter of commercial "
        "bargain and that neither party may seek to vary it by reference to the open market "
        "rent or any index.",
        "The Landlord shall notify the Tenant of the reviewed Rent in writing not less than one "
        "month before the Review Date, but a failure to do so shall not affect the Tenant's "
        "obligation to pay the reviewed Rent from the Review Date.",
    ),
    "none": ("The Rent is not subject to review during the Term.",),
}

_REPAIR = (
    "The Tenant shall keep the Premises clean and tidy and shall decorate the interior of the "
    "Premises in every fifth year of the Term and in the last three months of the Term, in a "
    "good and workmanlike manner and with materials of good quality.",
    "The Tenant shall not make any structural alteration or addition to the Premises, nor any "
    "alteration to the exterior or the appearance of the Premises, nor any internal "
    "non-structural alteration without the prior written consent of the Landlord, which shall "
    "not be unreasonably withheld.",
    "The Tenant shall, on being given notice by the Landlord of any breach of the Tenant's "
    "repairing obligations, remedy the breach within two months or sooner if necessary, and if "
    "the Tenant fails to do so the Landlord may enter the Premises and carry out the work and "
    "recover the cost from the Tenant as a debt.",
    "The Tenant shall comply with all statutes, regulations and requirements of any competent "
    "authority relating to the Premises or their use, and shall indemnify the Landlord against "
    "all liability arising from any breach.",
    "The Tenant shall not do anything which would overload the floors, the structure or the "
    "Conduits of the Building, and shall not bring onto the Premises any machinery or equipment "
    "which is unduly heavy or which causes vibration.",
    "The Tenant shall at the end of the Term yield up the Premises in the state of repair and "
    "condition required by this Lease, having removed any alterations which the Landlord "
    "requires to be removed and made good any damage caused by their removal.",
    "The Tenant shall keep any plate glass in the Premises insured against breakage in the "
    "joint names of the Landlord and the Tenant and shall replace any broken glass with glass "
    "of the same quality as soon as reasonably practicable.",
    "The Landlord shall not be liable to the Tenant for any failure or interruption of any of "
    "the services provided to the Building which is caused by circumstances beyond the "
    "Landlord's reasonable control, provided that the Landlord uses reasonable endeavours to "
    "restore the service.",
    "The Tenant shall keep the Premises free from vermin and infestation and shall pay the cost "
    "of any treatment which the Landlord reasonably requires.",
    "Any damage to the Premises caused by an Insured Risk shall be excluded from the Tenant's "
    "repairing obligations unless the insurance has been vitiated by the act or default of the "
    "Tenant.",
)
_REPAIR_BY_BASIS = {
    "full_repairing": (
        "The Tenant shall keep the whole of the Premises, including the structure, the roof, the "
        "foundations, the exterior and the Conduits exclusively serving the Premises, in good "
        "and substantial repair and condition, and shall rebuild, renew or replace any part "
        "which cannot be repaired.",
        "The Tenant's obligation extends to defects which exist at the date of this Lease and to "
        "inherent defects in the design or construction of the Premises, whether or not they "
        "were known to either party at that date.",
    ),
    "internal_repairing": (
        "The Tenant shall keep the interior of the Premises, including the internal surfaces of "
        "the structural walls, the ceilings, the floor finishes, the doors and windows and the "
        "fixtures and fittings, in good repair and condition.",
        "The Landlord shall keep the structure, the roof, the foundations and the exterior of "
        "the Building, and the Common Parts and the Conduits which do not exclusively serve the "
        "Premises, in good repair and condition, and shall recover the cost through the Service "
        "Charge.",
    ),
    "landlord_repairing": (
        "The Landlord shall keep the Premises and the Building, including the structure, the "
        "exterior, the roof and the Conduits, in good repair and condition, and shall carry out "
        "any repair within a reasonable time of becoming aware that it is needed.",
        "The Tenant shall not damage the Premises, shall keep them clean, and shall promptly "
        "notify the Landlord of any want of repair of which the Tenant becomes aware.",
    ),
}

_USE = (
    "The Tenant shall not use the Premises for any purpose other than the Permitted Use.",
    "The Tenant shall not use the Premises for any illegal or immoral purpose, nor for any "
    "auction, public meeting or show, nor for any purpose which is noisy, offensive or "
    "dangerous or which causes a nuisance to the Landlord or to the tenants or occupiers of "
    "adjoining premises.",
    "The Tenant shall not apply for planning permission for any change of use of the Premises "
    "without the prior written consent of the Landlord.",
    "The Tenant shall not display any sign, advertisement or notice on the exterior of the "
    "Premises or in the windows so as to be visible from outside without the prior written "
    "consent of the Landlord, which shall not be unreasonably withheld in the case of a sign "
    "displaying the Tenant's name and business.",
    "The Tenant shall not permit any person to sleep on the Premises, nor allow the Premises to "
    "be used for residential purposes.",
    "The Tenant shall keep the Premises open for business during the usual business hours of "
    "the locality unless prevented by circumstances beyond the Tenant's control.",
    "The Tenant shall not store on the Premises any hazardous, flammable or explosive "
    "substance, other than reasonable quantities of cleaning materials and fuel for permitted "
    "equipment stored in accordance with the relevant regulations.",
    "The Tenant shall comply with all reasonable regulations made by the Landlord from time to "
    "time for the management of the Building and the Common Parts, provided that they have "
    "been notified to the Tenant in writing.",
    "Nothing in this Lease constitutes a warranty by the Landlord that the Premises may "
    "lawfully be used for the Permitted Use, and the Tenant has satisfied itself on that point "
    "before entering into this Lease.",
    "The Tenant shall not load or unload goods except at the loading areas designated by the "
    "Landlord and shall not park vehicles other than in the spaces, if any, allocated to the "
    "Premises.",
    "The Tenant shall not play or permit to be played any music or sound amplification which "
    "is audible outside the Premises.",
)

_ALIENATION = (
    "The Tenant shall not assign, underlet, charge, part with possession or share occupation of "
    "the whole or any part of the Premises except as expressly permitted by this clause.",
    "The Tenant may assign the whole of the Premises with the prior written consent of the "
    "Landlord, which shall not be unreasonably withheld or delayed.",
    "The Landlord may withhold consent to an assignment if the proposed assignee is not, in the "
    "Landlord's reasonable opinion, of sufficient financial standing to comply with the "
    "Tenant's covenants, or if there are arrears of Rent or any other subsisting breach of the "
    "Tenant's obligations.",
    "It shall be a condition of any assignment that the Tenant enters into an authorised "
    "guarantee agreement in the form reasonably required by the Landlord, guaranteeing the "
    "performance of the Tenant's covenants by the assignee.",
    "The Tenant may underlet the whole of the Premises with the prior written consent of the "
    "Landlord, at a rent not less than the open market rent of the Premises, without taking a "
    "fine or premium, and by an underlease which excludes the security of tenure provisions of "
    "the 1954 Act.",
    "The Tenant shall not underlet any part of the Premises, nor grant any right of occupation "
    "of part of the Premises to any person.",
    "The Tenant may share occupation of the Premises with a Group Company, provided that no "
    "relationship of landlord and tenant is created and that the Tenant notifies the Landlord "
    "of the arrangement in writing before it begins.",
    "Within one month after any assignment, underletting, charge or devolution of the Premises, "
    "the Tenant shall give the Landlord notice of it together with a certified copy of the "
    "relevant document and shall pay the Landlord's reasonable registration fee.",
    "The Tenant shall not charge any part of the Premises, but may charge the whole of the "
    "Premises with the prior written consent of the Landlord, which shall not be unreasonably "
    "withheld.",
    "The Landlord shall be entitled to require, as a condition of giving consent to an "
    "assignment, that the assignee provides a guarantor or a rent deposit acceptable to the "
    "Landlord where the assignee's financial standing reasonably requires it.",
    "Any consent given by the Landlord under this clause shall be given by deed and may be "
    "given subject to reasonable conditions.",
    "For the purposes of section 19(1A) of the Landlord and Tenant (Covenants) Act 1995, the "
    "parties agree that the conditions and circumstances set out in this clause are reasonable.",
)

_INSURANCE = (
    "The Landlord shall insure the Building, other than tenant's fixtures and fittings, against "
    "the Insured Risks in the full cost of reinstatement, with a reputable insurer and subject "
    "to such excesses, exclusions and conditions as the insurer may require.",
    "The Tenant shall pay the Insurance Rent within fourteen days of demand, the first payment "
    "being a proportionate sum for the period from the Term Commencement Date to the next "
    "renewal date of the policy.",
    "If the Premises or the Building are damaged or destroyed by an Insured Risk so as to be "
    "unfit for occupation and use, the Rent, or a fair proportion of it according to the extent "
    "of the damage, shall be suspended until the Premises are again fit for occupation and use "
    "or until the expiry of the period for which loss of Rent is insured, whichever is the "
    "earlier.",
    "The Landlord shall use the insurance money received, other than in respect of loss of "
    "Rent, to reinstate the Premises as soon as reasonably practicable, subject to obtaining "
    "all necessary consents and to the Landlord not being prevented by any cause beyond its "
    "control.",
    "If the Premises have not been reinstated so as to be fit for occupation and use within "
    "three years after the date of the damage, either party may end this Lease by giving the "
    "other not less than one month's written notice, and the insurance money shall belong to "
    "the Landlord.",
    "The Tenant shall not do anything at the Premises which would make the insurance of the "
    "Building void or voidable or which would increase the premium, and shall pay any increased "
    "premium caused by the Tenant's use of the Premises.",
    "The Tenant shall comply with all requirements and recommendations of the insurer and shall "
    "notify the Landlord as soon as it becomes aware of any damage to the Premises by an "
    "Insured Risk.",
    "If the insurance money is irrecoverable in whole or in part because of any act or default "
    "of the Tenant, the Tenant shall pay the Landlord the irrecoverable amount on demand.",
    "The Landlord shall, on request and not more than once in any year, produce to the Tenant "
    "evidence of the terms of the insurance policy and of payment of the current premium.",
    "The Tenant shall insure its own fixtures, fittings, stock and contents and shall maintain "
    "public liability insurance in respect of the Premises with a limit of indemnity of not "
    "less than five million pounds for any one occurrence.",
    "Where damage is caused by an Uninsured Risk, the Landlord may elect, by notice to the "
    "Tenant within six months of the damage, either to reinstate the Premises at its own cost "
    "or to end this Lease, in which case the Rent shall be suspended from the date of the "
    "damage.",
)

_BREAK = (
    "The break notice shall be served in writing in accordance with the notice provisions of "
    "this Lease and shall be irrevocable once served.",
    "The Tenant may exercise the break only if, on the break date, the Tenant has paid all of "
    "the Rent which has fallen due and gives vacant possession of the whole of the Premises to "
    "the Landlord.",
    "The Landlord may waive any condition of the break by notice in writing to the Tenant, but "
    "no condition shall be treated as waived by conduct.",
    "If the break is validly exercised, this Lease shall end on the break date without prejudice "
    "to any right or remedy of either party in respect of any earlier breach of the other's "
    "obligations.",
    "Any Rent paid in advance for a period after the break date shall be refunded to the Tenant "
    "within one month after the break date.",
    "The right to determine this Lease under this clause is personal to the Tenant named in "
    "this Lease and to any Group Company to which the Lease has been assigned with the "
    "Landlord's consent.",
    "Time shall be of the essence for the service of the break notice and for the satisfaction "
    "of the conditions attached to it.",
)

_DEPOSIT = (
    "The Landlord shall hold the Deposit in a separate interest-bearing account as security for "
    "the performance of the Tenant's obligations under this Lease, and any interest earned "
    "shall be added to the Deposit.",
    "If the Tenant fails to pay any Rent or other sum within fourteen days after it falls due, "
    "or is otherwise in breach of this Lease, the Landlord may withdraw from the Deposit such "
    "amount as is required to make good the default and shall give the Tenant written notice "
    "of the withdrawal.",
    "Within fourteen days after receiving a notice of withdrawal, the Tenant shall pay to the "
    "Landlord a sum sufficient to restore the Deposit to its original amount.",
    "The Landlord shall release the balance of the Deposit to the Tenant within one month after "
    "the end of the Term, or after any earlier lawful assignment of this Lease, provided that "
    "the Tenant has complied with its obligations and there are no outstanding claims by the "
    "Landlord.",
    "The Deposit shall not be treated as a payment of Rent in advance, and the Tenant shall not "
    "withhold any payment of Rent on the ground that the Landlord holds the Deposit.",
    "If the Landlord assigns its interest in the Premises, it shall transfer the Deposit to the "
    "assignee and shall procure that the assignee holds it on the terms of this clause, "
    "whereupon the Landlord shall be released from its obligations in respect of the Deposit.",
    "The Landlord's rights under this clause are in addition to and not in substitution for "
    "any other right or remedy available to the Landlord.",
)

_GUARANTEE = (
    "The Guarantor guarantees to the Landlord that the Tenant shall pay the Rent and all other "
    "sums payable under this Lease and shall perform and observe the Tenant's covenants, and "
    "the Guarantor shall indemnify the Landlord against all losses arising from any default by "
    "the Tenant.",
    "The Guarantor's liability shall not be released, reduced or affected by any time or "
    "indulgence granted by the Landlord to the Tenant, by any variation of this Lease, or by "
    "any change in the constitution of the Tenant or the Guarantor.",
    "If this Lease is disclaimed on the insolvency of the Tenant, the Guarantor shall, if the "
    "Landlord so requires within six months after the disclaimer, accept a new lease of the "
    "Premises for the residue of the Term on the same terms as this Lease.",
    "The Guarantor waives any right to require the Landlord to proceed against the Tenant or to "
    "pursue any other remedy before enforcing the guarantee.",
    "The guarantee shall continue until the Tenant is released from its obligations under this "
    "Lease by statute, and shall then extend to any authorised guarantee agreement entered into "
    "by the Tenant.",
    "Any payment made by the Guarantor shall be made without set-off or counterclaim, and the "
    "Guarantor shall not claim in competition with the Landlord in the insolvency of the Tenant "
    "until the Landlord has been paid in full.",
)

_VAT = (
    "The Tenant shall pay VAT on the Rent and on every other sum payable under this Lease where "
    "VAT is chargeable, at the same time as the sum on which it is chargeable, against a valid "
    "VAT invoice.",
    "Every sum payable under this Lease is expressed exclusive of VAT, and any reference in this "
    "Lease to a sum, a cost or an expense includes any VAT which is payable on it and which is "
    "not recoverable by the payer.",
    "Nothing in this Lease shall prevent the Landlord from making, revoking or varying an "
    "election to waive the exemption from VAT in respect of the Building, and the Landlord "
    "shall notify the Tenant in writing within one month of doing so.",
    "If the Tenant is not registered for VAT or is unable to recover VAT in full, that shall not "
    "relieve the Tenant of the obligation to pay VAT in accordance with this clause.",
    "The Landlord shall provide the Tenant with a valid VAT invoice for every payment of Rent "
    "and other sums on which VAT is chargeable.",
    "Where the Landlord has waived the exemption from VAT, the Tenant shall not do anything "
    "which would cause the waiver to be disapplied, and shall indemnify the Landlord against "
    "any loss arising from a breach of this obligation.",
)

_NOTICES = (
    "Any notice under this Lease shall be in writing and shall be served by hand or by recorded "
    "delivery post at the registered office of the party to be served, or at such other address "
    "as that party may have notified in writing for the purpose.",
    "A notice served by post shall be treated as served on the second Working Day after "
    "posting, and a notice served by hand shall be treated as served on the day it is "
    "delivered, or on the next Working Day if delivered after four o'clock in the afternoon.",
    "Notice may not be validly served by email or by any other electronic means unless the "
    "parties have agreed in writing to accept service in that manner.",
    "This Lease is governed by the law of England and Wales, and the courts of England and "
    "Wales have exclusive jurisdiction to determine any dispute arising out of it.",
    "A person who is not a party to this Lease has no right under the Contracts (Rights of "
    "Third Parties) Act 1999 to enforce any of its terms.",
    "This Lease, together with any documents annexed to it, contains the whole agreement "
    "between the parties relating to the Premises and supersedes any earlier agreement, "
    "arrangement or understanding between them.",
    "The Landlord shall be entitled to re-enter the Premises and end this Lease if the Rent or "
    "any other sum is unpaid for twenty-one days after falling due, whether formally demanded "
    "or not, or if the Tenant is in breach of any of its obligations, or if the Tenant becomes "
    "insolvent.",
    "The Tenant shall pay the Landlord's reasonable costs, including legal and surveyor's fees, "
    "incurred in connection with any application for consent under this Lease, whether or not "
    "consent is granted, and in the preparation and service of any notice under section 146 of "
    "the Law of Property Act 1925.",
    "The Tenant shall give the Landlord written notice of any change to the Tenant's registered "
    "office or principal place of business within fourteen days of the change.",
    "Nothing in this Lease shall give the Tenant any right to renew it, and the Tenant "
    "acknowledges that it has taken independent legal advice on the effect of the exclusion of "
    "the security of tenure provisions of the 1954 Act.",
)

_SCHEDULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Rights Granted",
        (
            "The right to pass and repass on foot and, where appropriate, with vehicles over the "
            "Common Parts and the access roads serving the Building for the purpose of gaining "
            "access to and egress from the Premises.",
            "The right to use the Conduits serving the Premises for the passage of Utilities, "
            "subject to the Landlord's right to re-route them on giving reasonable notice.",
            "The right of support and protection for the Premises from the other parts of the "
            "Building.",
            "The right to use the car parking spaces, if any, allocated to the Premises by the "
            "Landlord from time to time, for the parking of private motor vehicles only.",
            "The right to display the Tenant's name on the directory board in the entrance hall "
            "of the Building in the style approved by the Landlord.",
            "The right to use the refuse storage area designated by the Landlord for the storage "
            "of refuse pending collection.",
            "The right, on reasonable notice and subject to making good all damage, to enter the "
            "Common Parts to carry out any works which the Tenant is obliged or permitted to "
            "carry out under this Lease.",
        ),
    ),
    (
        "Rights Reserved",
        (
            "The right to enter the Premises on reasonable notice, or without notice in an "
            "emergency, to inspect, repair, clean, alter or renew any adjoining premises or the "
            "Conduits.",
            "The right to build on, alter or develop any adjoining property of the Landlord in "
            "any manner, even if the access of light or air to the Premises is reduced, provided "
            "that the Tenant's use of the Premises is not materially interfered with.",
            "The right of support and protection for the other parts of the Building from the "
            "Premises.",
            "The right to use the Conduits in the Premises for the passage of Utilities to and "
            "from other parts of the Building.",
            "The right to erect scaffolding on or against the Building for the purpose of "
            "repairing or cleaning it, provided that access to the Premises is maintained.",
            "The right to fix and maintain on the exterior of the Premises any sign, aerial, "
            "satellite dish or plant serving the Building, provided that it does not materially "
            "interfere with the Tenant's use of the Premises.",
            "The right, during the last six months of the Term, to enter the Premises with "
            "prospective tenants and to display a notice advertising the Premises for letting.",
        ),
    ),
    (
        "Services",
        (
            "Repairing, maintaining, cleaning, lighting and, where appropriate, heating the "
            "Common Parts and the structure and exterior of the Building.",
            "Providing, maintaining and renewing lifts, fire alarms, security systems and other "
            "plant serving the Building.",
            "Insuring the Building and the plant in it against the Insured Risks and against "
            "third party liability, so far as not recovered through the Insurance Rent.",
            "Employing staff, contractors and managing agents for the provision of the services "
            "and the management of the Building, and paying the fees of the Landlord's Surveyor "
            "and accountants in connection with the Service Charge.",
            "Providing refuse collection, pest control, window cleaning and landscaping services "
            "to the Building and its grounds.",
            "Complying with any statutory requirement relating to the Building or the Common "
            "Parts and with the requirements of the insurers.",
            "Maintaining a reserve fund for the anticipated cost of replacing plant and carrying "
            "out major works of repair to the Building over the Term.",
            "Any other service which the Landlord reasonably considers to be for the benefit of "
            "the tenants and occupiers of the Building generally.",
        ),
    ),
)

#: Where each distractor is printed.
_DISTRACTOR_CLAUSE = {
    "rent_commencement_date": "Rent",
    "service_charge_cap": "Rent",
    "previous_rent": "Rent",
    "insurance_rent_estimate": "Insurance",
    "managing_agent": "Notices",
}


# -------------------------------------------------------------- rendering --

_BODY = ParagraphStyle(
    "body", fontName="Times-Roman", fontSize=11, leading=16, spaceAfter=7, alignment=TA_JUSTIFY
)
_HEADING = ParagraphStyle(
    "heading", fontName="Times-Bold", fontSize=12, leading=16, spaceBefore=12, spaceAfter=6
)
_COVER = ParagraphStyle(
    "cover", fontName="Times-Bold", fontSize=22, leading=28, alignment=TA_CENTER, spaceAfter=18
)
_COVER_LINE = ParagraphStyle(
    "cover-line", fontName="Times-Roman", fontSize=13, leading=19, alignment=TA_CENTER, spaceAfter=8
)


def _paragraphs(rng: random.Random, pool: tuple[str, ...], fraction: float) -> list[str]:
    """A fresh selection and grouping of a clause's sentences. Which sentences
    appear, in what order and how many to a paragraph all vary, which is what
    moves the fact sentences around from lease to lease."""
    count = max(2, min(len(pool), round(len(pool) * fraction)))
    chosen = rng.sample(pool, count)
    paragraphs: list[str] = []
    index = 0
    while index < len(chosen):
        size = rng.choice((1, 2, 2, 3))
        paragraphs.append(" ".join(chosen[index : index + size]))
        index += size
    return paragraphs


def _interleave(
    rng: random.Random, boilerplate: list[str], facts: list[str]
) -> list[tuple[str, bool]]:
    """Slot the fact sentences among the boilerplate, in their own order and
    never first: a fact directly under a heading would be glued to it when the
    page text is read back, and a clause that opens with its boilerplate reads
    more like a lease anyway."""
    slots = sorted(rng.choices(range(1, len(boilerplate) + 1), k=len(facts)))
    result: list[tuple[str, bool]] = []
    fact_index = 0
    for position, text in enumerate(boilerplate, start=1):
        result.append((text, False))
        while fact_index < len(facts) and slots[fact_index] == position:
            result.append((facts[fact_index], True))
            fact_index += 1
    return result


def _clause(number: int, title: str, paragraphs: list[tuple[str, bool]]) -> list:
    # Headings end in a full stop so the sentence splitter keeps them apart
    # from the first paragraph beneath them.
    flow: list = [Paragraph(escape(f"{number}. {title}."), _HEADING)]
    for index, (text, is_fact) in enumerate(paragraphs, start=1):
        paragraph = Paragraph(escape(f"{number}.{index} {text}"), _BODY)
        # A fact sentence split across a page break would be on neither page.
        flow.append(KeepTogether(paragraph) if is_fact else paragraph)
    return flow


def _schedule(number: int, title: str, items: list[str]) -> list:
    flow: list = [Paragraph(escape(f"Schedule {number}. {title}."), _HEADING)]
    for index, text in enumerate(items, start=1):
        flow.append(Paragraph(escape(f"{index}. {text}"), _BODY))
    return flow


def _cover(spec: LeaseSpec) -> list:
    return [
        Spacer(1, 150),
        Paragraph("LEASE", _COVER),
        Paragraph(escape(f"of {spec.unit}, {spec.property_name}."), _COVER_LINE),
        Paragraph(escape(f"Dated {format_date(spec.lease_date)}."), _COVER_LINE),
        Spacer(1, 60),
        Paragraph(escape("Hallam & Pryce, managing agents for the Landlord."), _COVER_LINE),
        Paragraph(escape(f"Reference {spec.reference}."), _COVER_LINE),
        PageBreak(),
    ]


def _definitions(rng: random.Random) -> list[tuple[str, bool]]:
    """The definitions every lease needs plus a random share of the rest, in
    alphabetical order, then a few rules of interpretation.

    Independent of the lease's overall verbosity on purpose: this clause is
    the one that decides which page the demise and the rent land on, and it
    should vary even between two otherwise short leases.
    """
    optional = [i for i in range(len(_DEFINITIONS)) if i not in _CORE_DEFINITIONS]
    extra = rng.randint(2, len(optional))
    chosen = sorted(set(_CORE_DEFINITIONS) | set(rng.sample(optional, extra)))
    paragraphs = [(_DEFINITIONS[i], False) for i in chosen]
    rules = _paragraphs(rng, _INTERPRETATION, rng.uniform(0.3, 1.0))
    paragraphs += [(text, False) for text in rules]
    return paragraphs


def _story(spec: LeaseSpec) -> list:
    rng = random.Random(f"render:{spec.reference}:{spec.tenant_name}")
    facts = fact_sentences(spec)
    distractors = {key: [] for key in ("Rent", "Insurance", "Notices")}
    for distractor in spec.distractors:
        distractors[_DISTRACTOR_CLAUSE[distractor.key]].append(distractor.sentence)
    # One verbosity per lease, jittered per clause, so short and long leases
    # both exist and the same clause is not always the long one.
    verbosity = rng.uniform(0.55, 1.0)

    def fraction() -> float:
        return min(1.0, max(0.3, verbosity + rng.uniform(-0.15, 0.15)))

    def body(pool: tuple[str, ...], fact_keys: list[str], extra: list[str] | None = None):
        boilerplate = _paragraphs(rng, pool, fraction())
        return _interleave(rng, boilerplate, [facts[k] for k in fact_keys] + (extra or []))

    story = _cover(spec)
    number = 0

    def clause(title: str, paragraphs: list[tuple[str, bool]]) -> None:
        nonlocal number
        number += 1
        story.extend(_clause(number, title, paragraphs))

    intro = (
        f"This Lease is made on {format_date(spec.lease_date)} between the parties described "
        "in this clause."
    )
    party_keys = ["landlord_name", "tenant_name"] + (["guarantor"] if spec.guarantor else [])
    clause(
        "Parties",
        _interleave(
            rng,
            [intro] + _paragraphs(rng, _PARTIES, fraction()),
            [facts[k] for k in party_keys],
        ),
    )
    clause("Definitions", _definitions(rng))
    clause("Demise and Term", body(_DEMISE, ["property_address", "unit", "term_years", "term"]))
    clause("Rent", body(_RENT, ["annual_rent_gbp"], distractors["Rent"]))
    clause(
        "Rent Review",
        body(
            _REVIEW + _REVIEW_BY_BASIS[spec.rent_review_basis],
            ["rent_review_basis", "rent_review_date"],
        ),
    )
    clause(
        "Repair",
        body(_REPAIR + _REPAIR_BY_BASIS[spec.repairing_obligation], ["repairing_obligation"]),
    )
    clause("Use", body(_USE, ["permitted_use"]))
    clause("Alienation", body(_ALIENATION, []))
    clause("Insurance", body(_INSURANCE, [], distractors["Insurance"]))
    if "break" in facts:
        clause("Break Clause", body(_BREAK, ["break"]))
    clause("Deposit", body(_DEPOSIT, ["deposit_gbp"]))
    if "guarantor" in facts:
        clause("Guarantee", body(_GUARANTEE, []))
    clause("VAT", body(_VAT, ["vat_elected"]))
    clause("Notices", body(_NOTICES, [], distractors["Notices"]))

    for index, (title, items) in enumerate(_SCHEDULES, start=1):
        count = max(3, round(len(items) * fraction()))
        story.extend(_schedule(index, title, rng.sample(items, count)))
    return story


def render_lease_pdf(spec: LeaseSpec) -> bytes:
    """The lease as a PDF. Same bytes for the same spec, every time."""
    buffer = io.BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=72,
        rightMargin=72,
        topMargin=72,
        bottomMargin=72,
        title=f"Lease of {spec.title}",
        author="Hallam & Pryce",
        subject=spec.reference,
        # Fixed creation date and no random document id: the sample PDFs are
        # committed, and a byte-for-byte reproducible file keeps them out of
        # every unrelated diff.
        invariant=1,
    )
    frame = Frame(
        document.leftMargin,
        document.bottomMargin,
        document.width,
        document.height,
        id="body",
    )

    def footer(canvas, doc) -> None:  # noqa: ANN001 - ReportLab callback
        canvas.saveState()
        canvas.setFont("Times-Roman", 9)
        canvas.drawRightString(
            A4[0] - document.rightMargin,
            document.bottomMargin - 30,
            f"Lease reference {spec.reference}. Page {doc.page}.",
        )
        canvas.restoreState()

    document.addPageTemplates([PageTemplate(id="lease", frames=[frame], onPageEnd=footer)])
    document.build(_story(spec))
    return buffer.getvalue()


# --------------------------------------------------------------- questions --

_QUESTIONS: dict[str, tuple[str, ...]] = {
    "term_end": (
        "When does the lease of {unit}, {property} expire?",
        "When does the term of the {tenant} lease at {property} end?",
        "What is the expiry date of the term granted by the lease of {unit} at {property}?",
        "On what date does the {tenant} lease of {unit}, {property} come to an end?",
    ),
    "term_start": (
        "When did the term of the lease of {unit}, {property} commence?",
        "What is the term commencement date for {unit} at {property}?",
        "From what date does {tenant} hold {unit}, {property}?",
    ),
    "term_years": (
        "How many years is the term of the lease of {unit}, {property}?",
        "What length of term was granted to {tenant} at {property}?",
    ),
    "annual_rent_gbp": (
        "What is the annual rent for {unit}, {property}?",
        "How much rent per annum does {tenant} pay for {unit} at {property}?",
        "What is the initial rent under the lease of {unit}, {property}?",
        "What rent is {tenant} paying at {property}?",
    ),
    "rent_review_basis": (
        "On what basis is the rent reviewed under the lease of {unit}, {property}?",
        "Is the rent review at {property} {unit} open market or index-linked?",
        "How is the rent payable by {tenant} at {property} reviewed?",
    ),
    "rent_review_date": (
        "When is the first rent review for {unit}, {property}?",
        "When is the rent for {unit} at {property} next reviewed?",
        "What is the review date under the {tenant} lease at {property}?",
    ),
    "break_date": (
        "When can the tenant break the lease of {unit}, {property}?",
        "Is there a break clause in the {tenant} lease at {property}, and when is the break date?",
        "On what date may {tenant} determine the lease of {unit} at {property}?",
    ),
    "break_notice_months": (
        "What notice does the tenant have to give to use the break at {property} {unit}?",
        "How many months' notice must {tenant} give to exercise the break clause at {property}?",
        "How much prior written notice is needed to operate the break in the lease of {unit}, "
        "{property}?",
    ),
    "tenant_name": (
        "Who is the tenant at {unit}, {property}?",
        "Who is the tenant under the lease of {unit} at {address}?",
        "Which company holds the lease of {unit}, {property}?",
    ),
    "landlord_name": (
        "Who is the landlord of {unit}, {property}?",
        "Which company is the landlord under the {tenant} lease at {property}?",
    ),
    "repairing_obligation": (
        "Is the lease of {unit}, {property} a full repairing lease?",
        "What is the repairing obligation under the {tenant} lease at {property}?",
        "On what repairing basis is {unit}, {property} let?",
    ),
    "permitted_use": (
        "What is the permitted use of {unit}, {property}?",
        "What use does the lease of {unit} at {property} allow?",
        "What can {tenant} use {unit}, {property} for under the lease?",
    ),
    "deposit_gbp": (
        "How much is the rent deposit for {unit}, {property}?",
        "What deposit did {tenant} pay under the lease of {unit} at {property}?",
    ),
    "guarantor": (
        "Who is the guarantor under the {tenant} lease at {property}?",
        "Is there a guarantor for the lease of {unit}, {property}, and who is it?",
    ),
    "vat_elected": (
        "Has the landlord elected to waive the VAT exemption at {property}?",
        "Is VAT payable on the rent for {unit}, {property}?",
        "Has the landlord opted to tax {property}?",
    ),
    "property_address": (
        "What is the full postal address of {property}, where {tenant} holds {unit}?",
    ),
    "unit": ("Which unit at {property} does {tenant} lease?",),
}

#: How often each field is asked about. The register's two quarterly
#: questions -- what expires when, where the breaks are -- lead.
_QUESTION_WEIGHTS = {
    "term_end": 5,
    "annual_rent_gbp": 5,
    "break_date": 4,
    "break_notice_months": 4,
    "tenant_name": 3,
    "rent_review_date": 3,
    "rent_review_basis": 2,
    "repairing_obligation": 2,
    "permitted_use": 2,
    "deposit_gbp": 2,
    "guarantor": 2,
    "vat_elected": 2,
    "landlord_name": 2,
    "term_start": 2,
    "term_years": 1,
    "property_address": 1,
    "unit": 1,
}

#: Things a lease never says. Twelve, filled in with real tenants and
#: buildings so they read like the questions people actually ask -- and so a
#: system that answers them is guessing.
UNANSWERABLE_QUESTIONS: tuple[str, ...] = (
    "What is the VAT registration number of {tenant}?",
    "How many employees does {tenant} have at {unit}, {property}?",
    "Who was the previous tenant of {unit}, {property}?",
    "What are the landlord's bank account details for paying the rent at {property}?",
    "Is {tenant} profitable?",
    "What is the floor area of {unit}, {property} in square feet?",
    "In what year was {property} built?",
    "Who is the solicitor acting for {tenant} on the lease of {unit}, {property}?",
    "What is the EPC rating of {unit}, {property}?",
    "Has {tenant} ever paid the rent at {property} late?",
    "What are the business rates payable on {unit}, {property}?",
    "Who signed the lease of {unit}, {property} on behalf of {tenant}?",
)


def _question_values(spec: LeaseSpec) -> dict[str, str]:
    return {
        "unit": spec.unit,
        "property": spec.property_name,
        "tenant": spec.tenant_name,
        "address": spec.property_address,
    }


def golden_questions(spec: LeaseSpec, pages: list[str]) -> list[GoldenQ]:
    """Three or four natural questions about the lease, each with the pages
    that answer it.

    The expected pages are found by looking for the canonical sentence in
    the extracted page text, not by remembering where the renderer put it:
    the evaluation should trust the PDF, not the generator.
    """
    rng = random.Random(f"questions:{spec.reference}:{spec.tenant_name}")
    sentences = fact_sentences(spec)
    candidates = [key for key in _QUESTION_WEIGHTS if FIELD_TEMPLATE[key] in sentences]
    weights = [_QUESTION_WEIGHTS[key] for key in candidates]

    chosen: list[str] = []
    wanted = rng.choice((3, 4, 4))
    while candidates and len(chosen) < wanted:
        key = rng.choices(candidates, weights=weights, k=1)[0]
        index = candidates.index(key)
        del candidates[index], weights[index]
        # One question per sentence: the term and the break each print two
        # facts in one sentence, and two questions with the same answer page
        # would be one test counted twice.
        if any(FIELD_TEMPLATE[key] == FIELD_TEMPLATE[c] for c in chosen):
            continue
        chosen.append(key)

    values = _question_values(spec)
    questions: list[GoldenQ] = []
    for key in chosen:
        sentence = sentences[FIELD_TEMPLATE[key]]
        expected = [number for number, page in enumerate(pages, start=1) if sentence in page]
        if not expected:
            continue
        template = rng.choice(_QUESTIONS[key])
        questions.append(GoldenQ(template.format(**values), True, expected, key))
    return questions


def unanswerable_questions(specs: list[LeaseSpec]) -> list[GoldenQ]:
    """The twelve unanswerable questions, each about one of ``specs``."""
    if not specs:
        return []
    return [
        GoldenQ(
            template.format(**_question_values(specs[index % len(specs)])),
            False,
            [],
            None,
        )
        for index, template in enumerate(UNANSWERABLE_QUESTIONS)
    ]


# ----------------------------------------------------------------- samples --

_SAMPLE_PROFILES: tuple[tuple[str, str], ...] = (
    ("with a break clause and a guarantor", "break_and_guarantor"),
    ("with neither a break clause nor a guarantor", "neither"),
    ("with an RPI-linked rent review", "rpi"),
)


def _matches(profile: str, spec: LeaseSpec) -> bool:
    if profile == "break_and_guarantor":
        return spec.break_date is not None and spec.guarantor is not None
    if profile == "neither":
        return spec.break_date is None and spec.guarantor is None
    return spec.rent_review_basis == "rpi"


def sample_specs(seed: int = DEFAULT_SEED) -> list[tuple[str, LeaseSpec]]:
    """(description, spec) for the three committed samples: the first demo
    lease that fits each profile, so the samples are also in the seeded set."""
    specs = generate_specs(DEMO_LEASE_COUNT, seed=seed)
    chosen: list[tuple[str, LeaseSpec]] = []
    for description, profile in _SAMPLE_PROFILES:
        taken = {spec.reference for _, spec in chosen}
        spec = next((s for s in specs if _matches(profile, s) and s.reference not in taken), None)
        if spec is None:
            raise ValueError(f"No generated lease {description} for seed {seed}.")
        chosen.append((description, spec))
    return chosen


def write_samples(directory: str | Path, seed: int = DEFAULT_SEED) -> list[Path]:
    """Write the three sample PDFs and a README describing them; return the
    PDF paths."""
    from app.services.pdf import extract_pages

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    sections: list[str] = []
    for index, (description, spec) in enumerate(sample_specs(seed)):
        data = render_lease_pdf(spec)
        path = target / spec.filename
        path.write_bytes(data)
        written.append(path)
        pages = extract_pages(data)
        sections.append(_readme_section(spec, description, pages, index))

    readme = target / "README.md"
    readme.write_text(_README_HEAD + "\n\n".join(sections) + "\n", encoding="utf-8")
    return written


_README_HEAD = """# Sample leases

Three of the forty-eight generated leases the demo is seeded with, committed so
the upload flow can be tried without running the seed. They are generated on
purpose and regenerated by `python -m app.demo.leases` from `api/`; the bytes
are reproducible, so regenerating them changes nothing unless the generator
changed.

Nothing in them is real. Hallam & Pryce, the landlords, the tenants and the
buildings are invented; only the streets and towns are Manchester's.

Each lease states every registry fact in one sentence, and the page that
sentence is printed on is listed below so an answer's citation can be checked
by eye. The questions at the end of each section are the ones the evaluation
set asks about that lease; the unanswerable one is there to see the honest
refusal.

"""


def _readme_section(spec: LeaseSpec, description: str, pages: list[str], index: int) -> str:
    sentences = fact_sentences(spec)

    def page_of(key: str) -> str:
        sentence = sentences.get(key)
        if sentence is None:
            return "not stated"
        found = [str(n) for n, page in enumerate(pages, start=1) if sentence in page]
        return f"page {', '.join(found)}" if found else "not found"

    money = format_money
    facts = [
        f"- Landlord: {spec.landlord_name} ({page_of('landlord_name')})",
        f"- Tenant: {spec.tenant_name} ({page_of('tenant_name')})",
        (
            f"- Guarantor: {spec.guarantor} ({page_of('guarantor')})"
            if spec.guarantor
            else "- Guarantor: none"
        ),
        f"- Term: {spec.term_years} years from {format_date(spec.term_start)} to "
        f"{format_date(spec.term_end)} ({page_of('term')})",
        f"- Rent: {money(spec.annual_rent_gbp)} per annum ({page_of('annual_rent_gbp')}), "
        f"reviewed on the {REVIEW_BASIS_TEXT[spec.rent_review_basis]} basis "
        f"({page_of('rent_review_basis')}) on {format_date(spec.rent_review_date)} "
        f"({page_of('rent_review_date')})",
        (
            f"- Break: {format_date(spec.break_date)} on {spec.break_notice_months} months' "
            f"notice ({page_of('break')})"
            if spec.break_date is not None
            else "- Break: none"
        ),
        f"- Repair: {REPAIR_TEXT[spec.repairing_obligation]} ({page_of('repairing_obligation')})",
        f"- Permitted use: {spec.permitted_use} ({page_of('permitted_use')})",
        f"- Deposit: {money(spec.deposit_gbp)} ({page_of('deposit_gbp')})",
        f"- VAT: landlord has {'elected' if spec.vat_elected else 'not elected'} to waive the "
        f"exemption ({page_of('vat_elected')})",
    ]
    questions = [
        f'- "{q.question}" (page {", ".join(str(p) for p in q.expected_pages)})'
        for q in golden_questions(spec, pages)
    ]
    unanswerable = UNANSWERABLE_QUESTIONS[index % len(UNANSWERABLE_QUESTIONS)].format(
        **_question_values(spec)
    )
    return "\n".join(
        [
            f"## `{spec.filename}`",
            "",
            f"{spec.title} ({spec.portfolio} portfolio, reference {spec.reference}): a lease "
            f"{description}, {len(pages)} pages.",
            "",
            *facts,
            "",
            "Questions to ask, with the page the answer is on:",
            "",
            *questions,
            f'- "{unanswerable}" (not answerable from the lease)',
        ]
    )


if __name__ == "__main__":
    import sys

    default = Path(__file__).resolve().parents[3] / "samples"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    for written in write_samples(out):
        print(written)
