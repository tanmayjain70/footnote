"""Shapes for the usage screen and the budget control.

Money fields are ``Decimal`` and serialise to JSON as strings, never numbers:
the API stores six decimal places and a JSON number would round them. The
web client renders the string as it is.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class UsageDay(BaseModel):
    day: date
    cost_usd: Decimal
    calls: int


class UsageByKind(BaseModel):
    kind: str
    cost_usd: Decimal
    calls: int


class UsageByUser(BaseModel):
    #: None for spend whose user has since been deleted; the cost still counts.
    user_id: uuid.UUID | None
    full_name: str | None
    cost_usd: Decimal
    calls: int


class UsageSummary(BaseModel):
    spent_today_usd: Decimal
    daily_budget_usd: Decimal
    remaining_usd: Decimal
    #: How many days ``by_day`` covers, today included. Every day is present,
    #: zero-filled, so the chart has no gaps to misread as missing data.
    days: int
    by_day: list[UsageDay]
    by_kind: list[UsageByKind]
    by_user: list[UsageByUser]


class BudgetRequest(BaseModel):
    #: Two decimal places because the client set the limit in dollars and
    #: cents; the spend it is compared against carries six. The range is
    #: checked by the service, not by ``ge``/``le`` here: pydantic reports
    #: those bounds as Decimals in the error context, and the API's one
    #: error shape is plain JSON.
    daily_budget_usd: Decimal = Field(max_digits=10, decimal_places=2)
