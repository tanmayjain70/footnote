"""Test fixtures.

The suite runs against a real PostgreSQL with pgvector, because the behaviour
under test -- hybrid retrieval, tsvector matching, the portfolio filter inside
the retrieval SQL, ``FOR UPDATE SKIP LOCKED`` claiming -- lives in the database
and a SQLite stand-in would quietly assume it works.

Requests go through ``fastapi.testclient.TestClient``: the API serves nothing
to itself, so there is no need for a live port. Isolation is by truncation
rather than a transaction per test, because ingestion commits mid-operation and
a rolled-back outer transaction would hide it.

Each agent running the suite in parallel points ``DATABASE_URL`` and
``DATABASE_ADMIN_URL`` at its own ``footnote_test_N`` database; the default is
``footnote_test``.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

# Must be set before anything imports app.core.config. The first three may be
# overridden from the environment; the rest are forced, because .env in api/
# is the demo configuration and the tests must not inherit it.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://footnote_app:app_dev_2026@localhost:5433/footnote_test",
)
os.environ.setdefault(
    "DATABASE_ADMIN_URL",
    "postgresql+psycopg://footnote_owner:owner_dev_2026@localhost:5433/footnote_test",
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-one-0123456789")
os.environ["ENVIRONMENT"] = "test"
# The worker thread would claim jobs underneath the tests; they drain the
# queue synchronously instead, with ingest_all().
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = "stub"
# Feature hashing: no model download, and the same vector every run.
os.environ["EMBEDDING_PROVIDER"] = "hashed"
os.environ["DEMO_MODE"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Portfolio, PortfolioMember, User  # noqa: E402
from app.models.enums import Role  # noqa: E402

API = "/api/v1"
TEST_PASSWORD = "test-password"

#: Every table except org_settings, which keeps its one row and has its
#: budget reset instead. Order does not matter under CASCADE.
TABLES = [
    "eval_results",
    "eval_runs",
    "eval_questions",
    "extracted_values",
    "extractions",
    "feedback",
    "citations",
    "question_sources",
    "questions",
    "usage_events",
    "jobs",
    "chunks",
    "document_pages",
    "document_blobs",
    "documents",
    "portfolio_members",
    "portfolios",
    "users",
]

#: One user per role, plus a second manager whose only portfolio is the
#: confidential one -- the access-control tests need somebody who can see it
#: and somebody who cannot.
USER_KEYS = ["director", "admin", "manager", "riverside_manager", "viewer"]
USER_ROLES = {
    "director": Role.DIRECTOR,
    "admin": Role.ADMIN,
    "manager": Role.MANAGER,
    "riverside_manager": Role.MANAGER,
    "viewer": Role.VIEWER,
}
USER_NAMES = {
    "director": "Priya Hallam",
    "admin": "Owen Pryce-Reid",
    "manager": "Sadia Mahmood",
    "riverside_manager": "Tom Whitlock",
    "viewer": "Grace Adeyemi",
}

#: Truncation connects as the owner: the runtime role has DML and no TRUNCATE,
#: and weakening production to suit the tests would be exactly backwards.
admin_engine = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)


@pytest.fixture(scope="session", autouse=True)
def migrate() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
def client(migrate) -> Iterator[TestClient]:
    """One client for the session. Entering the context runs the lifespan,
    which is where the least-privilege check on the runtime role happens."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean(migrate) -> Iterator[None]:
    with admin_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                """
                INSERT INTO org_settings (id, org_name, daily_budget_usd)
                VALUES (1, 'Hallam & Pryce', 5.00)
                ON CONFLICT (id) DO UPDATE
                    SET org_name = EXCLUDED.org_name,
                        daily_budget_usd = EXCLUDED.daily_budget_usd,
                        updated_at = now()
                """
            )
        )
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def users(db: Session) -> dict[str, User]:
    made: dict[str, User] = {}
    for key in USER_KEYS:
        user = User(
            email=f"{key}@test.demo",
            full_name=USER_NAMES[key],
            role=USER_ROLES[key].value,
            password_hash=hash_password(TEST_PASSWORD),
        )
        db.add(user)
        made[key] = user
    db.commit()
    for user in made.values():
        db.refresh(user)
    return made


@pytest.fixture
def portfolios(db: Session, users: dict[str, User]) -> dict[str, Portfolio]:
    """Three portfolios with the memberships the brief describes.

    ``riverside`` is the one being sold: confidential, visible to the director
    (who sees everything) and to one manager. The admin is a member of all
    three, the other manager of the two ordinary ones, the viewer of one.
    """
    made = {
        "city": Portfolio(name="City Centre", slug="city-centre"),
        "north": Portfolio(name="Northern Estates", slug="northern-estates"),
        "riverside": Portfolio(name="Riverside", slug="riverside", confidential=True),
    }
    db.add_all(made.values())
    db.flush()

    memberships = {
        "admin": ["city", "north", "riverside"],
        "manager": ["city", "north"],
        "riverside_manager": ["riverside"],
        "viewer": ["city"],
    }
    for user_key, portfolio_keys in memberships.items():
        for portfolio_key in portfolio_keys:
            db.add(
                PortfolioMember(
                    user_id=users[user_key].id, portfolio_id=made[portfolio_key].id
                )
            )
    db.commit()
    for portfolio in made.values():
        db.refresh(portfolio)
    return made


@pytest.fixture
def auth(client: TestClient, users: dict[str, User]) -> Callable[..., dict[str, str]]:
    """``auth("admin")`` -> Authorization headers for that user."""

    def _login(role: str = "director") -> dict[str, str]:
        response = client.post(
            f"{API}/auth/login",
            json={"email": f"{role}@test.demo", "password": TEST_PASSWORD},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login


def build_pdf(pages: list[str]) -> bytes:
    """A real PDF with one page per string, so the parser under test is the
    real parser and the page numbers in citations are real page numbers."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    width, height = A4
    margin, leading, font, size = 56, 14, "Helvetica", 11
    for page in pages:
        pdf.setFont(font, size)
        y = height - margin
        for paragraph in page.split("\n"):
            for line in simpleSplit(paragraph, font, size, width - 2 * margin) or [""]:
                if y < margin:
                    # Overflow starts a new physical page; tests keep pages
                    # short enough that this does not happen by accident.
                    pdf.showPage()
                    pdf.setFont(font, size)
                    y = height - margin
                pdf.drawString(margin, y, line)
                y -= leading
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@pytest.fixture
def make_pdf() -> Callable[[list[str]], bytes]:
    return build_pdf


@pytest.fixture
def upload(
    client: TestClient, auth: Callable[..., dict[str, str]], portfolios: dict[str, Portfolio]
) -> Callable[..., dict[str, Any]]:
    """Upload a PDF through the API and return the DocumentOut.

    Calls ``POST /api/v1/documents`` as specified in SPEC section 8; the
    documents router is owned by the ingestion agent. A duplicate upload comes
    back as ``{"document": ..., "created": false}`` and is unwrapped here so
    callers always get the document.
    """

    def _upload(
        role: str,
        portfolio: Portfolio | uuid.UUID | str,
        pages: list[str],
        title: str | None = None,
        doc_type: str | None = None,
        filename: str = "lease.pdf",
    ) -> dict[str, Any]:
        portfolio_id = getattr(portfolio, "id", portfolio)
        if isinstance(portfolio, str) and portfolio in portfolios:
            portfolio_id = portfolios[portfolio].id
        data: dict[str, str] = {"portfolio_id": str(portfolio_id)}
        if title is not None:
            data["title"] = title
        if doc_type is not None:
            data["doc_type"] = doc_type
        response = client.post(
            f"{API}/documents",
            headers=auth(role),
            files={"file": (filename, build_pdf(pages), "application/pdf")},
            data=data,
        )
        assert response.status_code in (200, 201), response.text
        body = response.json()
        return body.get("document", body)

    return _upload


@pytest.fixture
def ingest_all(db: Session) -> Callable[..., int]:
    """Drain the job queue synchronously, as the worker would.

    Delegates to ``app.services.ingestion.process_pending`` (SPEC section 7),
    owned by the ingestion agent. Runs on the test's own session, so refresh
    any ORM objects you are holding afterwards.
    """

    def _run(**kwargs: Any) -> int:
        from app.services import ingestion

        return ingestion.process_pending(db, **kwargs)

    return _run
