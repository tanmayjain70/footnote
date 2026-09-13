"""Spend, the budget that caps it, and the screen that shows it.

The budget test is the one the brief cares about: with the limit reached,
asking costs nothing -- no question row, no call to the model -- and the
caller gets a 429 they can read, not half a stream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BudgetExhausted
from app.models import Extraction, OrgSettings, Question, UsageEvent
from app.providers import llm, pricing
from app.providers.llm import Usage
from app.providers.pricing import cost_usd
from app.services import usage
from tests.conftest import API
from tests.test_ask import BREAK_QUESTION, ScriptedLLM, parse_sse
from tests.test_retrieval import BREAK_CLAUSE, RENT_CLAUSE, TERM_CLAUSE, ready_document


@pytest.fixture
def lease(db: Session, portfolios):
    return ready_document(
        db, portfolios["city"], "Meridian House", [[TERM_CLAUSE, RENT_CLAUSE], [BREAK_CLAUSE]]
    )


def _event(db: Session, **overrides) -> UsageEvent:
    fields = {
        "kind": "answer",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cost_usd": Decimal("0.007500"),
    }
    fields.update(overrides)
    event = UsageEvent(**fields)
    db.add(event)
    db.commit()
    return event


def _set_budget(db: Session, amount: str) -> None:
    db.get(OrgSettings, 1).daily_budget_usd = Decimal(amount)
    db.commit()


# --- recording ----------------------------------------------------------


def test_a_priced_model_writes_a_priced_usage_row(
    client: TestClient, auth, lease, db: Session, monkeypatch
):
    """The stub does the answering but reports a real model, so the row is
    priced from the table and the question carries the same figure."""
    provider = llm.get_llm_provider()
    monkeypatch.setattr(provider, "model", "claude-opus-5")

    done = dict(
        parse_sse(
            client.post(
                f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION}
            ).text
        )
    )["done"]

    event = db.execute(select(UsageEvent)).scalar_one()
    expected = cost_usd(
        "claude-opus-5",
        Usage(input_tokens=event.input_tokens, output_tokens=event.output_tokens),
    )
    assert expected > 0, "the stub reports a token estimate, so a priced model costs something"
    assert event.cost_usd == expected
    assert event.model == "claude-opus-5"
    assert event.kind == "answer"
    assert str(event.reference_id) == done["id"]
    assert event.user_id is not None
    assert Decimal(done["cost_usd"]) == expected
    assert done["model"] == "claude-opus-5"
    assert done["input_tokens"] == event.input_tokens


def test_the_stub_costs_nothing_but_is_still_recorded(client: TestClient, auth, lease, db: Session):
    client.post(f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION})

    event = db.execute(select(UsageEvent)).scalar_one()
    assert event.cost_usd == Decimal("0")
    assert event.model == "extractive-v1"
    assert event.input_tokens > 0


def test_record_prices_from_the_table(db: Session, users):
    event = usage.record(
        db,
        user_id=users["manager"].id,
        kind="extraction",
        provider="anthropic",
        model="claude-sonnet-5",
        usage=Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000),
        reference_id=None,
    )
    db.commit()

    assert event.cost_usd == Decimal("2.20")
    assert usage.spent_today(db) == Decimal("2.200000")


# --- the budget ---------------------------------------------------------


def test_a_spent_budget_refuses_before_anything_happens(
    client: TestClient, auth, lease, db: Session, monkeypatch
):
    scripted = ScriptedLLM([])
    monkeypatch.setattr(llm, "get_llm_provider", lambda: scripted)
    _set_budget(db, "0")

    streamed = client.post(f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION})
    waited = client.post(
        f"{API}/ask",
        headers=auth("manager"),
        json={"question": BREAK_QUESTION},
        params={"stream": "false"},
    )

    for response in (streamed, waited):
        assert response.status_code == 429, response.text
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()["error"]
        assert body["code"] == "budget_exhausted"
        assert "midnight UTC" in body["message"]
        assert body["detail"]["daily_budget_usd"] == "0.00"
    assert db.execute(select(func.count()).select_from(Question)).scalar_one() == 0
    assert scripted.calls == 0


def test_the_budget_is_reached_when_spend_equals_it(db: Session):
    _set_budget(db, "0.50")
    _event(db, cost_usd=Decimal("0.25"))
    usage.check_budget(db)

    _event(db, cost_usd=Decimal("0.25"))
    with pytest.raises(BudgetExhausted) as raised:
        usage.check_budget(db)

    assert raised.value.status_code == 429
    assert "$0.50" in raised.value.message


def test_spent_today_counts_only_today(db: Session):
    _event(db, cost_usd=Decimal("1.50"), created_at=datetime.now(UTC) - timedelta(days=1))
    _event(db, cost_usd=Decimal("0.25"))

    assert usage.spent_today(db) == Decimal("0.250000")


def test_a_raised_budget_lets_the_next_question_through(
    client: TestClient, auth, lease, db: Session
):
    _set_budget(db, "0")
    assert (
        client.post(f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION})
    ).status_code == 429

    client.patch(f"{API}/usage/budget", headers=auth("director"), json={"daily_budget_usd": "1"})

    assert (
        client.post(f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION})
    ).status_code == 200


# --- the summary --------------------------------------------------------


def test_the_summary_has_the_shape_the_screen_reads(client: TestClient, auth, lease, db: Session):
    client.post(f"{API}/ask", headers=auth("manager"), json={"question": BREAK_QUESTION})
    _event(db, cost_usd=Decimal("0.30"), created_at=datetime.now(UTC) - timedelta(days=3))
    _event(db, cost_usd=Decimal("0.20"), kind="extraction")

    response = client.get(f"{API}/usage/summary", headers=auth("director"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "spent_today_usd",
        "daily_budget_usd",
        "remaining_usd",
        "days",
        "by_day",
        "by_kind",
        "by_user",
    }
    assert body["days"] == 14
    assert len(body["by_day"]) == 14
    assert body["by_day"][-1]["day"] == datetime.now(UTC).date().isoformat()
    assert body["by_day"][-1]["calls"] == 2
    assert body["by_day"][-4]["cost_usd"] == "0.300000"
    assert sum(day["calls"] for day in body["by_day"]) == 3

    assert body["spent_today_usd"] == "0.200000"
    assert body["daily_budget_usd"] == "5.00"
    assert body["remaining_usd"] == "4.800000"
    for key in ("spent_today_usd", "daily_budget_usd", "remaining_usd"):
        assert isinstance(body[key], str), "money is a decimal string, never a float"

    kinds = {row["kind"]: row for row in body["by_kind"]}
    assert set(kinds) == {"answer", "extraction"}
    assert kinds["answer"]["calls"] == 2

    users = {row["full_name"]: row for row in body["by_user"]}
    assert users["Sadia Mahmood"]["calls"] == 1
    assert None in users, "spend with no user still counts"


def test_the_summary_window_is_adjustable(client: TestClient, auth, db: Session):
    body = client.get(f"{API}/usage/summary", headers=auth("director"), params={"days": 7}).json()

    assert body["days"] == 7 and len(body["by_day"]) == 7


def test_the_summary_is_director_only(client: TestClient, auth):
    for role in ("admin", "manager", "viewer"):
        assert client.get(f"{API}/usage/summary", headers=auth(role)).status_code == 403


def test_the_budget_is_set_by_the_director_only(client: TestClient, auth, db: Session):
    refused = client.patch(
        f"{API}/usage/budget", headers=auth("admin"), json={"daily_budget_usd": "12.50"}
    )
    changed = client.patch(
        f"{API}/usage/budget", headers=auth("director"), json={"daily_budget_usd": "12.50"}
    )
    negative = client.patch(
        f"{API}/usage/budget", headers=auth("director"), json={"daily_budget_usd": "-1"}
    )

    assert refused.status_code == 403
    assert changed.status_code == 200, changed.text
    assert changed.json()["daily_budget_usd"] == "12.50"
    assert negative.status_code == 422
    assert usage.daily_budget(db) == Decimal("12.50")
    health = client.get(f"{API}/health", headers=auth("director")).json()
    assert health["budget"]["daily_budget_usd"] == "12.50"


# --- what a call is priced as -------------------------------------------


def test_a_dated_release_is_priced_as_its_family():
    """An alias is asked for and a dated release answers: `claude-opus-5` in,
    `claude-opus-5-20260401` out, and it is the answer that is recorded. An
    exact-match price table misses it, every call costs zero, and the daily
    budget never fires again -- which is the one failure this must not have.
    """
    tokens = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    alias = pricing.cost_usd("claude-opus-5", tokens)
    dated = pricing.cost_usd("claude-opus-5-20260401", tokens)

    assert alias > 0
    assert dated == alias
    assert pricing.cost_usd("claude-sonnet-5-20260101", tokens) == pricing.cost_usd(
        "claude-sonnet-5", tokens
    )
    # A model from nobody's price list still costs nothing, visibly.
    assert pricing.cost_usd("some-other-model", tokens) == 0


def test_extraction_asks_the_budget_before_it_spends(
    client: TestClient, auth, lease, db: Session
):
    """Answering is not the only thing that bills. Before this, an exhausted
    budget stopped questions and let somebody extract forty leases."""
    _set_budget(db, "0")

    response = client.post(f"{API}/documents/{lease.id}/extract", headers=auth("admin"))

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "budget_exhausted"
    assert db.execute(select(func.count()).select_from(Extraction)).scalar_one() == 0
