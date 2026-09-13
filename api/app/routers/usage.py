"""Model spend and the daily budget. Director only.

The budget is the client's number, so changing it is a deliberate act by the
one person who owns it, and the response to a change is the whole summary
again -- the screen that made the change is the screen that shows it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.deps import require_usage
from app.models import User
from app.schemas.usage import BudgetRequest, UsageSummary
from app.services import usage

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/summary", response_model=UsageSummary)
def usage_summary(
    days: int = Query(default=14, ge=1, le=90),
    db: Session = Depends(get_db),
    _: User = Depends(require_usage),
) -> UsageSummary:
    return UsageSummary(**usage.summary(db, days=days))


@router.patch("/budget", response_model=UsageSummary)
def set_budget(
    body: BudgetRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_usage),
) -> UsageSummary:
    """Takes effect on the next question: the check reads the row each time."""
    usage.set_budget(db, body.daily_budget_usd)
    return UsageSummary(**usage.summary(db))
