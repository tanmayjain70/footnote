from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import Unauthorized
from app.core.security import DUMMY_HASH, create_token, decode_token, verify_password
from app.deps import current_user, permissions_for, visible_portfolio_ids
from app.models import Document, Portfolio, User
from app.schemas.auth import (
    LoginRequest,
    MeOut,
    PermissionsOut,
    PortfolioSummary,
    RefreshRequest,
    TokenPair,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(user: User) -> TokenPair:
    settings = get_settings()
    return TokenPair(
        access_token=create_token(user.id, "access", {"role": user.role}),
        refresh_token=create_token(user.id, "refresh"),
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/login", response_model=TokenPair)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenPair:
    # ``first`` rather than ``scalar_one_or_none``: the unique index is on the
    # address as stored, so two rows differing only in case are possible, and
    # a 500 on the login route would be a poor way to find that out.
    user = db.execute(
        select(User).where(func.lower(User.email) == body.email.lower()).order_by(User.created_at)
    ).scalars().first()

    # One message for every failure, and a password hash is verified on every
    # path -- including the one where the account is disabled -- so the
    # response time does not say which of them happened.
    if user is None or not user.is_active:
        verify_password(body.password, DUMMY_HASH)
        raise Unauthorized("That email and password do not match.")
    if not verify_password(body.password, user.password_hash):
        raise Unauthorized("That email and password do not match.")

    return _tokens(user)


@router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except jwt.InvalidTokenError:
        raise Unauthorized("That refresh token is not valid.") from None

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("This account is no longer active.")
    return _tokens(user)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    visible = visible_portfolio_ids(db, user)
    counts = dict(
        db.execute(
            select(Document.portfolio_id, func.count())
            .where(Document.portfolio_id.in_(visible))
            .group_by(Document.portfolio_id)
        ).all()
    )
    portfolios = db.execute(
        select(Portfolio).where(Portfolio.id.in_(visible)).order_by(Portfolio.name)
    ).scalars()

    return MeOut(
        user=UserOut.model_validate(user),
        permissions=PermissionsOut(**permissions_for(user)),
        portfolios=[
            PortfolioSummary(
                id=p.id,
                name=p.name,
                slug=p.slug,
                confidential=p.confidential,
                document_count=counts.get(p.id, 0),
            )
            for p in portfolios
        ],
    )
