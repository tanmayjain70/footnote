from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str


class PermissionsOut(BaseModel):
    """What this person is allowed to do, resolved server-side.

    The frontend uses it to decide what to render; the API enforces it again
    on every request regardless.
    """

    can_upload: bool
    can_ask: bool
    can_review: bool
    can_run_evals: bool
    can_see_usage: bool


class PortfolioSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    confidential: bool
    document_count: int


class MeOut(BaseModel):
    user: UserOut
    permissions: PermissionsOut
    #: The portfolios this person can see, so the scope selector on the ask
    #: screen never offers one they cannot search.
    portfolios: list[PortfolioSummary]
