"""Model spend: recording it, summing it, and refusing to go past the limit.

The client set a daily budget in dollars and expects it to hold. It holds
here, server-side, in the one place every paid call passes through: a call is
priced from the token counts the provider reports and written as a row, and
the check before the next call sums today's rows. However many browser tabs
are open, the sum is the sum.

Days are UTC days because that is when the budget resets. A local-time day
would give a Manchester user a different limit in summer and in winter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.core.errors import HTTP_422, AppError, BudgetExhausted
from app.models import OrgSettings, UsageEvent, User
from app.providers.llm import Usage
from app.providers.pricing import cost_usd

#: Costs carry six places, as the provider bills them; the budget carries two,
#: as the client set it.
SIX_DP = Decimal("0.000001")
TWO_DP = Decimal("0.01")

#: The one row of organisation settings. The first migration inserts it.
ORG_ROW_ID = 1

#: A zero budget is a valid way to switch the model off; a budget above this
#: is a typo. The column holds ten digits, two of them after the point.
MAX_BUDGET_USD = Decimal("100000")


class InvalidBudget(AppError):
    status_code = HTTP_422
    code = "invalid_budget"


def record(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    kind: str,
    provider: str,
    model: str,
    usage: Usage,
    reference_id: uuid.UUID | None,
    commit: bool = False,
) -> UsageEvent:
    """Write one priced usage row.

    Flushed rather than committed, so the caller commits it with the question
    or extraction it paid for. Where that commit might not happen -- an answer
    that fails after the model has already billed for it -- the caller passes
    ``commit=True``: the money left whether or not the answer arrived, and the
    daily budget is only honest if it knows.
    """
    event = UsageEvent(
        user_id=user_id,
        kind=str(kind),
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=cost_usd(model, usage),
        reference_id=reference_id,
    )
    db.add(event)
    db.flush()
    if commit:
        db.commit()
    return event


def utc_day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(db: Session) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.created_at >= utc_day_start()
        )
    ).scalar_one()
    return Decimal(total).quantize(SIX_DP)


def daily_budget(db: Session) -> Decimal:
    org = db.get(OrgSettings, ORG_ROW_ID)
    if org is None:
        raise RuntimeError("org_settings row 1 is missing; the first migration inserts it.")
    return Decimal(org.daily_budget_usd).quantize(TWO_DP)


def check_budget(db: Session) -> None:
    """Raise ``BudgetExhausted`` when today's spend has reached the limit.

    Called before a request is sent, never after: once tokens are spent the
    money is gone, and the point of the check is that it is not.
    """
    spent = spent_today(db)
    budget = daily_budget(db)
    if spent >= budget:
        raise BudgetExhausted(
            f"Today's model spend of ${spent:.2f} has reached the daily limit of "
            f"${budget:.2f}. The budget resets at midnight UTC.",
            detail={"spent_today_usd": str(spent), "daily_budget_usd": str(budget)},
        )


def set_budget(db: Session, amount: Decimal) -> Decimal:
    amount = Decimal(amount)
    if not amount.is_finite() or amount < 0 or amount > MAX_BUDGET_USD:
        raise InvalidBudget(
            f"The daily budget must be between $0 and ${MAX_BUDGET_USD:,.2f}.",
            detail={"daily_budget_usd": str(amount)},
        )
    org = db.get(OrgSettings, ORG_ROW_ID)
    if org is None:
        raise RuntimeError("org_settings row 1 is missing; the first migration inserts it.")
    org.daily_budget_usd = amount.quantize(TWO_DP)
    db.commit()
    return org.daily_budget_usd


def summary(db: Session, days: int = 14) -> dict[str, Any]:
    """Today against the budget, and the last ``days`` days by day, by kind of
    call and by person. Every day in the window is present, zero-filled, so a
    quiet day reads as quiet rather than as missing."""
    today = datetime.now(UTC).date()
    first_day = today - timedelta(days=days - 1)
    window_start = datetime.combine(first_day, time.min, tzinfo=UTC)

    # created_at is timestamptz; group by its UTC calendar date, not the
    # session's local one, or the day boundary would move with the server.
    day_col = cast(func.timezone("UTC", UsageEvent.created_at), Date)
    by_day_rows = db.execute(
        select(day_col, func.sum(UsageEvent.cost_usd), func.count())
        .where(UsageEvent.created_at >= window_start)
        .group_by(day_col)
    ).all()
    per_day: dict[date, tuple[Decimal, int]] = {
        day: (Decimal(cost), calls) for day, cost, calls in by_day_rows
    }
    by_day = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        cost, calls = per_day.get(day, (Decimal(0), 0))
        by_day.append({"day": day, "cost_usd": cost.quantize(SIX_DP), "calls": calls})

    by_kind = [
        {"kind": kind, "cost_usd": Decimal(cost).quantize(SIX_DP), "calls": calls}
        for kind, cost, calls in db.execute(
            select(UsageEvent.kind, func.sum(UsageEvent.cost_usd), func.count())
            .where(UsageEvent.created_at >= window_start)
            .group_by(UsageEvent.kind)
            .order_by(func.sum(UsageEvent.cost_usd).desc(), UsageEvent.kind)
        ).all()
    ]

    by_user = [
        {
            "user_id": user_id,
            "full_name": full_name,
            "cost_usd": Decimal(cost).quantize(SIX_DP),
            "calls": calls,
        }
        for user_id, full_name, cost, calls in db.execute(
            select(
                UsageEvent.user_id,
                User.full_name,
                func.sum(UsageEvent.cost_usd),
                func.count(),
            )
            .outerjoin(User, User.id == UsageEvent.user_id)
            .where(UsageEvent.created_at >= window_start)
            .group_by(UsageEvent.user_id, User.full_name)
            .order_by(func.sum(UsageEvent.cost_usd).desc(), User.full_name)
        ).all()
    ]

    spent = spent_today(db)
    budget = daily_budget(db)
    return {
        "spent_today_usd": spent,
        "daily_budget_usd": budget,
        "remaining_usd": max(budget - spent, Decimal(0)).quantize(SIX_DP),
        "days": days,
        "by_day": by_day,
        "by_kind": by_kind,
        "by_user": by_user,
    }
