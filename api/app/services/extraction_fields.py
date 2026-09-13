"""The registry of lease terms the extraction asks for, and how a value is read.

Seventeen fields, in the order the review screen and the register show them.
Adding a field is appending a ``FieldSpec``; nothing else in the application
enumerates them by name.

Two things here are coordinated with other modules and tested together:

- ``stub_pattern`` is written against the exact canonical sentence the demo
  lease generator (``app.demo.leases.CLAUSE_TEMPLATES``) prints for that
  fact. The extractive stub applies it to every chunk in order, so a pattern
  that also matched boilerplate would quote the wrong page. Each one is
  anchored on wording that appears once per lease and nowhere else.
- ``parse_value`` is the single reader of a value, whether the model wrote it
  or a reviewer typed a correction. The same forms are accepted on both
  paths, so a correction the reviewer copies from the page parses exactly
  as the model's would have.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

from app.models.enums import ValueIssue

FieldType = Literal["text", "date", "money", "integer", "bool", "enum"]

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: FieldType
    description: str
    enum_values: tuple[str, ...] = ()
    stub_pattern: str = ""


# Fragments the stub patterns share. Dates are printed as "1 April 2024" and
# sums as "£42,500" throughout the generated leases; the fragments match that
# and nothing looser, so a pattern cannot drift onto a clause number.
_DATE = (
    r"\d{1,2} (?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{4}"
)
_MONEY = r"£[\d,]+"
_ENTITY = r"(?:a company|a limited liability partnership) registered in England and Wales"

FIELDS: list[FieldSpec] = [
    FieldSpec(
        key="tenant_name",
        label="Tenant",
        type="text",
        description=(
            "Full legal name of the tenant as given in the parties clause, including any "
            "suffix such as Limited, plc or LLP. Not the guarantor and not the managing agent."
        ),
        stub_pattern=rf"The Tenant is (?P<value>[^,]+), {_ENTITY}",
    ),
    FieldSpec(
        key="landlord_name",
        label="Landlord",
        type="text",
        description=(
            "Full legal name of the landlord as given in the parties clause. Not the "
            "managing agent, a superior landlord or the insurer."
        ),
        stub_pattern=rf"The Landlord is (?P<value>[^,]+), {_ENTITY}",
    ),
    FieldSpec(
        key="property_address",
        label="Property address",
        type="text",
        description=(
            "Postal address of the building the premises are in, as written in the lease, "
            "including the postcode. Not the registered office of either party."
        ),
        stub_pattern=r"The Building is (?P<value>[^.]+?)\.",
    ),
    FieldSpec(
        key="unit",
        label="Unit",
        type="text",
        description=(
            "The unit, suite or floor let by the lease, as the lease names it (for example "
            "'Unit 4' or 'Third Floor'), without the building name or address."
        ),
        stub_pattern=r"The Premises are known as (?P<value>[^,]+), ",
    ),
    FieldSpec(
        key="term_start",
        label="Term start",
        type="date",
        description=(
            "The date the contractual term commences. Not the date the lease was signed and "
            "not the rent commencement date."
        ),
        stub_pattern=(
            rf"The Term shall commence on (?P<value>{_DATE}) and shall expire on {_DATE}\."
        ),
    ),
    FieldSpec(
        key="term_end",
        label="Term end",
        type="date",
        description=(
            "The date the contractual term expires, as written. If the lease states only the "
            "length of the term, leave this empty rather than calculating it."
        ),
        stub_pattern=(
            rf"The Term shall commence on {_DATE} and shall expire on (?P<value>{_DATE})\."
        ),
    ),
    FieldSpec(
        key="term_years",
        label="Term (years)",
        type="integer",
        description="The length of the contractual term in whole years, as a number.",
        stub_pattern=r"let for a term of (?P<value>\d+) years \(the Term\)",
    ),
    FieldSpec(
        key="annual_rent_gbp",
        label="Annual rent (£)",
        type="money",
        description=(
            "The initial annual rent in pounds sterling, exclusive of VAT and before any "
            "rent-free period. Not the deposit, the service charge, the insurance rent or a "
            "rent under any earlier lease."
        ),
        stub_pattern=rf"The Initial Rent is (?P<value>{_MONEY}) \(",
    ),
    FieldSpec(
        key="rent_review_basis",
        label="Rent review basis",
        type="enum",
        description=(
            "How the rent is reviewed: open_market (to the market rent), rpi or cpi "
            "(index-linked), fixed_uplift (a stated increase), or none (not reviewed during "
            "the term)."
        ),
        enum_values=("open_market", "rpi", "cpi", "fixed_uplift", "none"),
        stub_pattern=r"The Review Basis is (?P<value>open market|RPI|CPI|fixed uplift|none)\b",
    ),
    FieldSpec(
        key="rent_review_date",
        label="Rent review date",
        type="date",
        description="The date of the first rent review. Leave empty when the rent is not reviewed.",
        stub_pattern=rf"The Review Date is (?P<value>{_DATE})\.",
    ),
    FieldSpec(
        key="break_date",
        label="Break date",
        type="date",
        description=(
            "The date on which the tenant may end the lease early under a break clause. "
            "Leave empty when the lease gives the tenant no break."
        ),
        stub_pattern=(
            rf"The Tenant may determine this Lease on (?P<value>{_DATE}) by giving the Landlord"
        ),
    ),
    FieldSpec(
        key="break_notice_months",
        label="Break notice (months)",
        type="integer",
        description=(
            "The minimum prior written notice, in months, the tenant must give to exercise "
            "the break. Leave empty when there is no break clause."
        ),
        stub_pattern=(
            rf"The Tenant may determine this Lease on {_DATE} by giving the Landlord not less "
            r"than (?P<value>\d+) months' prior written notice"
        ),
    ),
    FieldSpec(
        key="repairing_obligation",
        label="Repairing obligation",
        type="enum",
        description=(
            "Who repairs what: full_repairing (the tenant repairs everything, structure and "
            "exterior included), internal_repairing (the tenant repairs the interior only), "
            "or landlord_repairing (the landlord repairs the premises)."
        ),
        enum_values=("full_repairing", "internal_repairing", "landlord_repairing"),
        stub_pattern=(
            r"The repairing basis of this Lease is "
            r"(?P<value>full repairing|internal repairing|landlord repairing)\."
        ),
    ),
    FieldSpec(
        key="permitted_use",
        label="Permitted use",
        type="text",
        description=(
            "The use the lease permits, as written, including any Use Classes Order "
            "reference. Not the tenant's trade as described elsewhere."
        ),
        stub_pattern=r"The Permitted Use is (?P<value>[^.]+)\.",
    ),
    FieldSpec(
        key="deposit_gbp",
        label="Deposit (£)",
        type="money",
        description=(
            "The rent deposit in pounds sterling. Leave empty when the lease has no deposit. "
            "Not the rent and not the service charge cap."
        ),
        stub_pattern=rf"The Deposit is (?P<value>{_MONEY}) \(",
    ),
    FieldSpec(
        key="guarantor",
        label="Guarantor",
        type="text",
        description=(
            "Full name of the guarantor named in the parties clause, whether a company or an "
            "individual. Leave empty when nobody guarantees the tenant's obligations."
        ),
        stub_pattern=r"The Guarantor is (?P<value>[^,]+), (?:a company registered|of )",
    ),
    FieldSpec(
        key="vat_elected",
        label="VAT elected",
        type="bool",
        description=(
            "Whether the landlord has elected to waive the VAT exemption (opted to tax) so that "
            "VAT is payable on the rent: yes or no."
        ),
        stub_pattern=(
            r"The Landlord has (?P<value>elected|not elected) to waive the exemption from VAT"
        ),
    ),
]

FIELD_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELDS}


def field_by_key(key: str) -> FieldSpec | None:
    return FIELD_BY_KEY.get(key)


def fields_as_dicts() -> list[dict[str, Any]]:
    """The registry as the API publishes it: no stub pattern, which is an
    implementation detail of one provider."""
    out: list[dict[str, Any]] = []
    for spec in FIELDS:
        item = asdict(spec)
        del item["stub_pattern"]
        item["enum_values"] = list(spec.enum_values)
        out.append(item)
    return out


# ---------------------------------------------------------------- parsing --

_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_MULTI_SPACE = re.compile(r"\s+")
#: Day-first everywhere: the leases are English and "01/04/2024" is the first
#: of April. An American reading is not offered even as a fallback.
_DATE_FORMATS = (
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

_MONEY_NOISE = re.compile(
    r"(?i)\b(?:per annum|per year|per month|a year|p\.a\.|pa|pcm|plus vat|exclusive of vat|"
    r"excluding vat|ex vat|exc vat|pounds|sterling|gbp)\b\.?"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_INTEGER = re.compile(
    r"(?i)(?P<value>\d+)\s*(?:calendar\s+)?(?:years?|yrs?|months?|mths?|weeks?|days?)?['’]?"
)
_TRUE = {"yes", "y", "true", "elected", "opted", "opted to tax", "has elected", "1"}
_FALSE = {"no", "n", "false", "not elected", "not opted", "has not elected", "0", "none"}
_ENUM_SEPARATORS = re.compile(r"[\s\-/]+")


def parse_value(spec: FieldSpec, value_text: str | None) -> tuple[Any, str | None]:
    """Read ``value_text`` as the field's type.

    Returns ``(value_json, None)`` on success -- an ISO date string, a float
    for money, an int, a bool, the canonical enum value, or stripped text --
    and ``(None, "unparseable")`` otherwise. Nothing is guessed: "ten years"
    is unparseable as an integer, and a date that is not clearly day-first
    or ISO is refused rather than read the American way.
    """
    text = _clean(value_text)
    if not text:
        return None, ValueIssue.UNPARSEABLE
    parsed = _PARSERS[spec.type](spec, text)
    if parsed is None:
        return None, ValueIssue.UNPARSEABLE
    return parsed, None


def _clean(value_text: str | None) -> str:
    text = _MULTI_SPACE.sub(" ", (value_text or "")).strip()
    # A model quoting a value often keeps the quotation marks or the final
    # full stop of the sentence it came from; neither is part of the value.
    return text.strip("\"'“”‘’. ")


def _parse_text(spec: FieldSpec, text: str) -> str | None:
    return text or None


def _parse_date(spec: FieldSpec, text: str) -> str | None:
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    candidate = _ORDINAL_SUFFIX.sub("", text).replace(",", " ")
    candidate = _MULTI_SPACE.sub(" ", candidate).strip()
    for prefix in ("on ", "the "):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix) :]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_money(spec: FieldSpec, text: str) -> float | None:
    candidate = text.replace("£", " ")
    candidate = _MONEY_NOISE.sub(" ", candidate)
    candidate = candidate.replace(",", "").strip(" .")
    if not _NUMBER.fullmatch(candidate):
        return None
    return float(candidate)


def _parse_integer(spec: FieldSpec, text: str) -> int | None:
    match = _INTEGER.fullmatch(text)
    return int(match.group("value")) if match else None


def _parse_bool(spec: FieldSpec, text: str) -> bool | None:
    key = _MULTI_SPACE.sub(" ", text.lower())
    if key in _TRUE:
        return True
    if key in _FALSE:
        return False
    return None


def _parse_enum(spec: FieldSpec, text: str) -> str | None:
    key = _ENUM_SEPARATORS.sub("_", text.strip().lower()).strip("_")
    return key if key in spec.enum_values else None


_PARSERS = {
    "text": _parse_text,
    "date": _parse_date,
    "money": _parse_money,
    "integer": _parse_integer,
    "bool": _parse_bool,
    "enum": _parse_enum,
}
