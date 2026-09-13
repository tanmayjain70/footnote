"""The register: what counts, what is filtered, what is exported, who sees it.

The rule under test most carefully is the first one. A value nobody has
confirmed is null in the register and absent from the CSV, however confident
the model was -- the brief's third requirement, enforced by the query.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, User
from app.services import extraction as extraction_service
from tests.conftest import API
from tests.test_extraction import SPECS, _truth, _with_break, extract, ready_lease, values_by_key

REGISTER_KEYS = list(extraction_service.REGISTER_FIELDS)


def users_admin(db: Session) -> User:
    """The lease administrator, as the review endpoint would supply them."""
    return db.execute(select(User).where(User.email == "admin@test.demo")).scalar_one()


@pytest.fixture
def leases(db: Session, users, portfolios) -> dict[str, tuple[Document, object]]:
    """Three extracted leases: one with a break in City Centre, one without in
    Northern Estates, and one in confidential Riverside. All values pending."""
    with_break = _with_break(SPECS, True)
    without_break = _with_break(SPECS, False)
    made = {
        "city": ready_lease(
            db, portfolios["city"].id, with_break, title="City lease", uploaded_by=users["admin"].id
        )[0],
        "north": ready_lease(
            db,
            portfolios["north"].id,
            without_break,
            title="North lease",
            uploaded_by=users["admin"].id,
        )[0],
        "riverside": ready_lease(
            db,
            portfolios["riverside"].id,
            SPECS[2],
            title="Riverside lease",
            uploaded_by=users["admin"].id,
        )[0],
    }
    specs = {"city": with_break, "north": without_break, "riverside": SPECS[2]}
    for document in made.values():
        extract(db, document, users["admin"])
    return {key: (made[key], specs[key]) for key in made}


def _confirm_all(db: Session, document: Document, user) -> None:
    extraction = extraction_service.latest_extraction(db, document)
    for value in values_by_key(db, extraction).values():
        extraction_service.review(db, value, user, action="confirm")


def _row(body: dict, title: str) -> dict:
    return next(row for row in body["rows"] if row["title"] == title)


def test_only_confirmed_and_corrected_values_count(
    client: TestClient, db: Session, auth, users, leases
):
    city, spec = leases["city"]
    values = values_by_key(db, extraction_service.latest_extraction(db, city))
    extraction_service.review(db, values["tenant_name"], users["admin"], action="confirm")
    extraction_service.review(
        db, values["annual_rent_gbp"], users["admin"], action="correct", corrected_text="£1,000"
    )
    extraction_service.review(db, values["unit"], users["admin"], action="reject")

    body = client.get(f"{API}/register", headers=auth("viewer")).json()
    row = _row(body, "City lease")
    assert set(row) >= set(REGISTER_KEYS) | {"document_id", "title", "portfolio", "review"}
    assert row["portfolio"] == "City Centre"
    assert row["tenant_name"] == _truth(spec, "tenant_name"), "confirmed: counts"
    assert row["annual_rent_gbp"] == 1000.0, "corrected: the correction counts"
    assert row["unit"] is None, "rejected: never"
    assert row["term_end"] is None, "pending: not yet"
    assert row["break_date"] is None
    assert row["review"] == {"confirmed": 1, "corrected": 1, "rejected": 1, "pending": 14}
    assert row["complete"] is False

    # The viewer is only a member of City Centre, so that is the whole register.
    assert [r["title"] for r in body["rows"]] == ["City lease"]
    assert body["counts"] == {"documents": 1, "complete": 0, "pending_values": 14}


def test_include_unreviewed_shows_pending_values_but_never_rejected_ones(
    client: TestClient, db: Session, auth, users, leases
):
    city, spec = leases["city"]
    values = values_by_key(db, extraction_service.latest_extraction(db, city))
    extraction_service.review(db, values["unit"], users["admin"], action="reject")

    body = client.get(f"{API}/register?include_unreviewed=true", headers=auth("director")).json()
    row = _row(body, "City lease")
    for key in REGISTER_KEYS:
        if key == "unit":
            assert row[key] is None, "a person said this one is wrong"
        else:
            assert row[key] == _truth(spec, key), key
    assert row["complete"] is False, "shown is not the same as confirmed"


def test_complete_once_every_value_with_a_value_is_reviewed(
    client: TestClient, db: Session, auth, users, leases
):
    north, _ = leases["north"]
    values = values_by_key(db, extraction_service.latest_extraction(db, north))
    # The lease has no break clause, so those two are null with no evidence.
    # Confirming everything the model did find is enough to be complete.
    for value in values.values():
        if value.value_json is not None:
            extraction_service.review(db, value, users["admin"], action="confirm")

    body = client.get(f"{API}/register", headers=auth("director")).json()
    row = _row(body, "North lease")
    assert row["complete"] is True
    assert row["review"]["pending"] == 2
    assert row["break_date"] is None and row["break_notice_months"] is None
    assert body["counts"]["complete"] == 1


def test_expiring_before_filters_on_the_shown_value(
    client: TestClient, db: Session, auth, users, leases
):
    city, city_spec = leases["city"]
    north, north_spec = leases["north"]
    _confirm_all(db, city, users["admin"])
    _confirm_all(db, north, users["admin"])

    city_end = date.fromisoformat(_truth(city_spec, "term_end"))
    north_end = date.fromisoformat(_truth(north_spec, "term_end"))
    earliest, latest = sorted([(city_end, "City lease"), (north_end, "North lease")])

    cutoff = earliest[0] + timedelta(days=1)
    body = client.get(
        f"{API}/register?expiring_before={cutoff.isoformat()}", headers=auth("manager")
    ).json()
    titles = [row["title"] for row in body["rows"]]
    assert earliest[1] in titles
    if latest[0] >= cutoff:
        assert latest[1] not in titles

    # Before anything expires: nothing.
    body = client.get(f"{API}/register?expiring_before=2000-01-01", headers=auth("manager")).json()
    assert body["rows"] == []

    # A pending term end does not count, so an unconfirmed lease never "expires".
    riverside, _ = leases["riverside"]
    body = client.get(f"{API}/register?expiring_before=2999-01-01", headers=auth("director")).json()
    assert "Riverside lease" not in [row["title"] for row in body["rows"]]
    body = client.get(
        f"{API}/register?expiring_before=2999-01-01&include_unreviewed=true",
        headers=auth("director"),
    ).json()
    assert "Riverside lease" in [row["title"] for row in body["rows"]]


def test_has_break_filters_on_the_shown_value(client: TestClient, db: Session, auth, users, leases):
    city, city_spec = leases["city"]
    north, _ = leases["north"]
    _confirm_all(db, city, users["admin"])
    _confirm_all(db, north, users["admin"])

    with_break = client.get(f"{API}/register?has_break=true", headers=auth("manager")).json()
    assert [row["title"] for row in with_break["rows"]] == ["City lease"]
    assert with_break["rows"][0]["break_date"] == _truth(city_spec, "break_date")
    assert with_break["rows"][0]["break_notice_months"] == _truth(city_spec, "break_notice_months")

    without = client.get(f"{API}/register?has_break=false", headers=auth("manager")).json()
    assert [row["title"] for row in without["rows"]] == ["North lease"]

    # Unconfirmed, the city lease's break does not exist yet.
    values = values_by_key(db, extraction_service.latest_extraction(db, city))
    values["break_date"].review_status = "pending"
    db.commit()
    with_break = client.get(f"{API}/register?has_break=true", headers=auth("manager")).json()
    assert with_break["rows"] == []


def test_register_respects_the_portfolio_rule(
    client: TestClient, db: Session, auth, users, leases, portfolios
):
    for document, _ in leases.values():
        _confirm_all(db, document, users["admin"])

    def titles(role: str, query: str = "") -> list[str]:
        response = client.get(f"{API}/register{query}", headers=auth(role))
        assert response.status_code == 200, response.text
        return sorted(row["title"] for row in response.json()["rows"])

    assert titles("director") == ["City lease", "North lease", "Riverside lease"]
    assert titles("admin") == ["City lease", "North lease", "Riverside lease"]
    assert titles("manager") == ["City lease", "North lease"]
    assert titles("riverside_manager") == ["Riverside lease"]
    assert titles("viewer") == ["City lease"]
    assert titles("manager", f"?portfolio_id={portfolios['north'].id}") == ["North lease"]

    # Asking for the confidential portfolio by id is a 404 for a non-member.
    response = client.get(
        f"{API}/register?portfolio_id={portfolios['riverside'].id}", headers=auth("manager")
    )
    assert response.status_code == 404
    response = client.get(
        f"{API}/register/export.csv?portfolio_id={portfolios['riverside'].id}",
        headers=auth("manager"),
    )
    assert response.status_code == 404


def test_csv_export_matches_the_register(client: TestClient, db: Session, auth, users, leases):
    city, city_spec = leases["city"]
    _confirm_all(db, city, users["admin"])

    response = client.get(f"{API}/register/export.csv", headers=auth("manager"))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="register-' in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    header, *lines = rows
    assert header[:3] == ["document_id", "title", "portfolio"]
    assert set(REGISTER_KEYS) <= set(header)
    assert len(lines) == 2, "one line per document the manager can see"

    by_title = {line[header.index("title")]: dict(zip(header, line, strict=True)) for line in lines}
    city_line = by_title["City lease"]
    assert city_line["document_id"] == str(city.id)
    assert city_line["tenant_name"] == _truth(city_spec, "tenant_name")
    assert city_line["term_end"] == _truth(city_spec, "term_end")
    assert city_line["annual_rent_gbp"] == f"{_truth(city_spec, 'annual_rent_gbp'):.2f}"
    assert city_line["complete"] == "yes"

    north_line = by_title["North lease"]
    assert north_line["tenant_name"] == "", "unconfirmed values are blank by default"
    assert north_line["pending"] == "17"

    # The export honours the same switch as the screen.
    response = client.get(
        f"{API}/register/export.csv?include_unreviewed=true", headers=auth("manager")
    )
    header, *lines = list(csv.reader(io.StringIO(response.text)))
    north_line = next(
        dict(zip(header, line, strict=True)) for line in lines if line[1] == "North lease"
    )
    assert north_line["tenant_name"] != ""

    # Filters too: a CSV of "leases with a break" is what a landlord asks for.
    response = client.get(f"{API}/register/export.csv?has_break=true", headers=auth("manager"))
    header, *lines = list(csv.reader(io.StringIO(response.text)))
    assert [line[1] for line in lines] == ["City lease"]


def test_register_is_empty_without_extractions(
    client: TestClient, db: Session, auth, users, portfolios
):
    ready_lease(
        db, portfolios["city"].id, SPECS[0], title="Unextracted", uploaded_by=users["admin"].id
    )
    body = client.get(f"{API}/register", headers=auth("director")).json()
    assert body == {"rows": [], "counts": {"documents": 0, "complete": 0, "pending_values": 0}}
    response = client.get(f"{API}/register/export.csv", headers=auth("director"))
    assert response.text.count("\n") == 1, "just the header"


def test_register_uses_the_newest_extraction(client: TestClient, db: Session, auth, users, leases):
    city, spec = leases["city"]
    _confirm_all(db, city, users["admin"])
    before = _row(client.get(f"{API}/register", headers=auth("director")).json(), "City lease")
    assert before["tenant_name"] == _truth(spec, "tenant_name")

    # Extracting again produces a fresh, unreviewed set; the register follows
    # it rather than the confirmed values of the run it replaced.
    extract(db, city, users["admin"])
    after = _row(client.get(f"{API}/register", headers=auth("director")).json(), "City lease")
    assert after["tenant_name"] is None
    assert after["review"]["pending"] == 17
    assert after["complete"] is False


def test_the_csv_does_not_hand_a_spreadsheet_a_formula(
    client: TestClient, auth, leases, db: Session
):
    """A reviewer's correction ends up in a file a landlord opens in Excel.
    A cell that starts with = is a formula there, so it is written as text."""
    document = leases["city"][0]
    extraction = latest = extraction_service.latest_extraction(db, document)
    assert latest is not None
    value = values_by_key(db, extraction)["unit"]
    extraction_service.review(
        db,
        value,
        users_admin(db),
        action="correct",
        corrected_text='=HYPERLINK("http://example.test","click")',
        note="pasted from the agent's spreadsheet",
    )

    csv_text = client.get(f"{API}/register/export.csv", headers=auth("director")).text

    assert "'=HYPERLINK" in csv_text
    assert "\n=HYPERLINK" not in csv_text
    assert ",=HYPERLINK" not in csv_text
