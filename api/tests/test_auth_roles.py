"""Auth, roles, and the portfolio access rule.

The rule under test most carefully here is the last one: a document in a
portfolio you are not a member of does not exist, as far as the API is
concerned. Not forbidden -- absent.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFound
from app.core.security import DUMMY_HASH, hash_password, verify_password
from app.deps import get_visible_document, permissions_for, visible_portfolio_ids
from app.models import Document, Job, User
from app.models.enums import (
    ASK_ROLES,
    EVAL_ROLES,
    REVIEW_ROLES,
    UPLOAD_ROLES,
    USAGE_ROLES,
    DocumentStatus,
    JobKind,
    JobStatus,
    Role,
)
from tests.conftest import API, TEST_PASSWORD

WRONG_CREDENTIALS = "That email and password do not match."


def _time_verify(digest: str) -> float:
    """How long one password comparison takes, best of three: the machine is
    shared with a database and a linter, so the fastest run is the honest one.
    """
    return min(
        _elapsed(lambda: verify_password("some password", digest)) for _ in range(3)
    )


def _elapsed(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def _document(portfolio_id: uuid.UUID, title: str, status: str = DocumentStatus.READY) -> Document:
    """A document row inserted directly: these tests are about visibility,
    not ingestion, and must not depend on the upload pipeline."""
    return Document(
        portfolio_id=portfolio_id,
        title=title,
        filename=f"{title.lower().replace(' ', '-')}.pdf",
        content_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        byte_size=1234,
        status=status,
    )


# --- login and tokens ---------------------------------------------------


def test_login_returns_a_token_pair(client: TestClient, users):
    response = client.post(
        f"{API}/auth/login", json={"email": "director@test.demo", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] == get_settings().access_token_minutes * 60


def test_login_email_is_case_insensitive(client: TestClient, users):
    response = client.post(
        f"{API}/auth/login", json={"email": "Director@Test.Demo", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_wrong_password_and_unknown_email_get_the_same_message(client: TestClient, users):
    wrong = client.post(
        f"{API}/auth/login", json={"email": "director@test.demo", "password": "nope"}
    )
    unknown = client.post(
        f"{API}/auth/login", json={"email": "nobody@test.demo", "password": TEST_PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == WRONG_CREDENTIALS
    assert unknown.json()["error"]["message"] == WRONG_CREDENTIALS
    assert wrong.json()["error"]["code"] == "unauthorized"


def test_inactive_account_cannot_sign_in(client: TestClient, db: Session, users):
    users["viewer"].is_active = False
    db.commit()
    response = client.post(
        f"{API}/auth/login", json={"email": "viewer@test.demo", "password": TEST_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == WRONG_CREDENTIALS


def test_refresh_issues_a_new_pair_that_works(client: TestClient, users):
    login = client.post(
        f"{API}/auth/login", json={"email": "admin@test.demo", "password": TEST_PASSWORD}
    ).json()

    refreshed = client.post(f"{API}/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["access_token"] != login["access_token"]

    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "admin@test.demo"


def test_token_kinds_are_not_interchangeable(client: TestClient, users):
    """A refresh token lives for two weeks; accepting it as an access token
    would make the short access lifetime decorative."""
    login = client.post(
        f"{API}/auth/login", json={"email": "admin@test.demo", "password": TEST_PASSWORD}
    ).json()

    as_access = client.get(
        f"{API}/auth/me", headers={"Authorization": f"Bearer {login['refresh_token']}"}
    )
    assert as_access.status_code == 401

    as_refresh = client.post(f"{API}/auth/refresh", json={"refresh_token": login["access_token"]})
    assert as_refresh.status_code == 401


def test_expired_token_has_its_own_error_code(client: TestClient, users):
    """The frontend refreshes on ``token_expired`` and signs out on anything
    else, so the two must be distinguishable without parsing prose."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(users["director"].id),
            "typ": "access",
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


def test_endpoints_need_a_token(client: TestClient):
    for path in ("/auth/me", "/portfolios", "/health"):
        assert client.get(f"{API}{path}").status_code == 401, path
    assert client.post(f"{API}/jobs/drain").status_code == 401


# --- permissions per role -----------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            "director",
            {
                "can_upload": True,
                "can_ask": True,
                "can_review": True,
                "can_run_evals": True,
                "can_see_usage": True,
            },
        ),
        (
            "admin",
            {
                "can_upload": True,
                "can_ask": True,
                "can_review": True,
                "can_run_evals": False,
                "can_see_usage": False,
            },
        ),
        (
            "manager",
            {
                "can_upload": False,
                "can_ask": True,
                "can_review": False,
                "can_run_evals": False,
                "can_see_usage": False,
            },
        ),
        (
            "viewer",
            {
                "can_upload": False,
                "can_ask": False,
                "can_review": False,
                "can_run_evals": False,
                "can_see_usage": False,
            },
        ),
    ],
)
def test_me_reports_the_permissions_of_the_role(client: TestClient, auth, role, expected):
    body = client.get(f"{API}/auth/me", headers=auth(role)).json()
    assert body["user"]["role"] == role
    assert body["user"]["email"] == f"{role}@test.demo"
    assert set(body["user"]) == {"id", "email", "full_name", "role"}
    assert body["permissions"] == expected


def test_permissions_for_matches_the_role_sets():
    """The dict the frontend gets is derived from the same sets the
    ``require_*`` dependencies enforce, so the two cannot drift apart."""
    for role in Role:
        user = User(email="x@test.demo", full_name="x", role=role.value, password_hash="x")
        assert permissions_for(user) == {
            "can_upload": role in UPLOAD_ROLES,
            "can_ask": role in ASK_ROLES,
            "can_review": role in REVIEW_ROLES,
            "can_run_evals": role in EVAL_ROLES,
            "can_see_usage": role in USAGE_ROLES,
        }


# --- portfolio visibility -----------------------------------------------


@pytest.mark.parametrize(
    ("role", "visible"),
    [
        ("director", {"City Centre", "Northern Estates", "Riverside"}),
        ("admin", {"City Centre", "Northern Estates", "Riverside"}),
        ("manager", {"City Centre", "Northern Estates"}),
        ("riverside_manager", {"Riverside"}),
        ("viewer", {"City Centre"}),
    ],
)
def test_me_lists_only_visible_portfolios(client: TestClient, auth, portfolios, role, visible):
    body = client.get(f"{API}/auth/me", headers=auth(role)).json()
    assert {p["name"] for p in body["portfolios"]} == visible
    for p in body["portfolios"]:
        assert set(p) == {"id", "name", "slug", "confidential", "document_count"}


def test_portfolio_listing_follows_membership_and_counts_documents(
    client: TestClient, db: Session, auth, portfolios
):
    city, riverside = portfolios["city"], portfolios["riverside"]
    db.add_all(
        [
            _document(city.id, "Unit 1 lease", DocumentStatus.READY),
            _document(city.id, "Unit 2 lease", DocumentStatus.QUEUED),
            _document(riverside.id, "Quay lease", DocumentStatus.READY),
        ]
    )
    db.commit()

    director = client.get(f"{API}/portfolios", headers=auth("director")).json()
    assert [p["name"] for p in director] == ["City Centre", "Northern Estates", "Riverside"]
    by_name = {p["name"]: p for p in director}
    assert (by_name["City Centre"]["document_count"], by_name["City Centre"]["ready_count"]) == (
        2,
        1,
    )
    assert by_name["Northern Estates"]["document_count"] == 0
    assert by_name["Riverside"]["confidential"] is True

    manager = client.get(f"{API}/portfolios", headers=auth("manager")).json()
    assert [p["name"] for p in manager] == ["City Centre", "Northern Estates"]

    riverside_manager = client.get(f"{API}/portfolios", headers=auth("riverside_manager")).json()
    assert [p["name"] for p in riverside_manager] == ["Riverside"]
    assert riverside_manager[0]["document_count"] == 1

    viewer = client.get(f"{API}/portfolios", headers=auth("viewer")).json()
    assert [p["name"] for p in viewer] == ["City Centre"]


def test_a_user_with_no_memberships_sees_nothing(client: TestClient, db: Session, auth, users):
    """No memberships is an empty list, not an error and not everything."""
    assert client.get(f"{API}/portfolios", headers=auth("viewer")).json() == []
    assert visible_portfolio_ids(db, users["viewer"]) == []


def test_visible_portfolio_ids_at_service_level(db: Session, users, portfolios):
    everything = {p.id for p in portfolios.values()}
    assert set(visible_portfolio_ids(db, users["director"])) == everything
    assert set(visible_portfolio_ids(db, users["admin"])) == everything
    assert set(visible_portfolio_ids(db, users["manager"])) == {
        portfolios["city"].id,
        portfolios["north"].id,
    }
    assert visible_portfolio_ids(db, users["riverside_manager"]) == [portfolios["riverside"].id]
    assert visible_portfolio_ids(db, users["viewer"]) == [portfolios["city"].id]


def test_document_outside_visible_set_is_404_not_403(db: Session, users, portfolios):
    """Confirming that a document exists in the confidential portfolio would
    tell a manager there is a lease they are not supposed to know about."""
    hidden = _document(portfolios["riverside"].id, "Quay lease")
    db.add(hidden)
    db.commit()

    assert get_visible_document(db, users["director"], hidden.id).id == hidden.id
    assert get_visible_document(db, users["riverside_manager"], hidden.id).id == hidden.id
    assert get_visible_document(db, users["admin"], hidden.id).id == hidden.id

    with pytest.raises(NotFound) as excluded:
        get_visible_document(db, users["manager"], hidden.id)
    with pytest.raises(NotFound) as missing:
        get_visible_document(db, users["manager"], uuid.uuid4())
    # Identical, on purpose: the response must not distinguish the two.
    assert excluded.value.message == missing.value.message
    assert excluded.value.status_code == 404

    with pytest.raises(NotFound):
        get_visible_document(db, users["viewer"], hidden.id)


# --- role gates on endpoints --------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "viewer"])
def test_only_the_director_can_drain_the_queue(client: TestClient, auth, role):
    response = client.post(f"{API}/jobs/drain", headers=auth(role))
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "forbidden"
    assert error["detail"] == {"your_role": role, "required": ["director"]}


def test_director_drains_an_empty_queue(client: TestClient, auth):
    pytest.importorskip("app.services.ingestion")
    response = client.post(f"{API}/jobs/drain", headers=auth("director"))
    assert response.status_code == 200, response.text
    assert response.json() == {"processed": 0}


# --- health -------------------------------------------------------------


def test_health_screen_shape(client: TestClient, auth):
    body = client.get(f"{API}/health", headers=auth("viewer")).json()
    assert body["worker_alive"] is False, "tests run with the worker disabled"
    assert body["jobs"] == {"queued": 0, "running": 0, "failed": 0}
    assert body["documents"] == {"ready": 0, "processing": 0, "failed": 0}
    assert body["llm"]["provider"] == "stub"
    assert body["llm"]["api_key_configured"] is False
    assert body["embeddings"]["dimensions"] == 384
    # What the firm spends is the director's business, and the header does not
    # read it. A viewer gets the screen without the money on it.
    assert body["budget"] is None

    director = client.get(f"{API}/health", headers=auth("director")).json()
    assert director["budget"] == {"daily_budget_usd": "5.00", "spent_today_usd": "0.000000"}


def test_health_counts_jobs_and_documents(client: TestClient, db: Session, auth, portfolios):
    failed = _document(portfolios["city"].id, "Broken scan", DocumentStatus.FAILED)
    ready = _document(portfolios["north"].id, "Good lease", DocumentStatus.READY)
    db.add_all([failed, ready])
    db.flush()
    db.add_all(
        [
            Job(kind=JobKind.INGEST, status=JobStatus.QUEUED, document_id=ready.id),
            Job(kind=JobKind.INGEST, status=JobStatus.FAILED, document_id=failed.id),
            Job(kind=JobKind.EXTRACT, status=JobStatus.DONE, document_id=ready.id),
        ]
    )
    db.commit()

    body = client.get(f"{API}/health", headers=auth("director")).json()
    assert body["jobs"] == {"queued": 1, "running": 0, "failed": 1}
    assert body["documents"] == {"ready": 1, "processing": 0, "failed": 1}


def test_the_dummy_hash_costs_a_real_comparison():
    """No account matching means a password is still verified, against a real
    digest. The obvious way to write that -- a made-up salt -- raises inside
    bcrypt and returns in microseconds, which tells anybody asking that the
    address does not exist."""
    assert verify_password("anything", DUMMY_HASH) is False

    # Same work either way: a genuine digest of the same cost.
    real = hash_password("a real password")
    dummy_time = _time_verify(DUMMY_HASH)
    real_time = _time_verify(real)
    assert dummy_time > 0.01, "the dummy comparison did no work"
    assert 0.5 < dummy_time / real_time < 2.0, (
        f"dummy {dummy_time:.3f}s against real {real_time:.3f}s leaks which is which"
    )


def test_a_disabled_account_is_refused_like_a_missing_one(client: TestClient, db: Session, users):
    users["manager"].is_active = False
    db.commit()

    disabled = client.post(
        f"{API}/auth/login", json={"email": "manager@test.demo", "password": TEST_PASSWORD}
    )
    missing = client.post(
        f"{API}/auth/login", json={"email": "nobody@test.demo", "password": TEST_PASSWORD}
    )

    assert disabled.status_code == missing.status_code == 401
    assert disabled.json()["error"]["message"] == missing.json()["error"]["message"]
