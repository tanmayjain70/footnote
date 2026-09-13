"""Request dependencies: who is calling, may they do this, and what may they see.

The portfolio rule lives here and nowhere else. A document outside the
caller's visible set is a 404, never a 403 -- a 403 would confirm that the
document exists, and for a portfolio being sold that is exactly the leak the
access control is there to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import Forbidden, NotFound, Unauthorized
from app.core.security import decode_token
from app.models import Document, Portfolio, PortfolioMember, User
from app.models.enums import (
    ASK_ROLES,
    EVAL_ROLES,
    REVIEW_ROLES,
    UPLOAD_ROLES,
    USAGE_ROLES,
    Role,
)


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Sign in to continue.")

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token, "access")
    except jwt.ExpiredSignatureError:
        raise Unauthorized("Your session has expired.", code="token_expired") from None
    except jwt.InvalidTokenError:
        raise Unauthorized("That token is not valid.") from None

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("This account is no longer active.")
    return user


def _require(roles: set[Role], what: str) -> Callable[[User], User]:
    allowed = {r.value for r in roles}

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise Forbidden(
                f"Your role cannot {what}.",
                detail={"your_role": user.role, "required": sorted(allowed)},
            )
        return user

    return dependency


require_upload = _require(UPLOAD_ROLES, "upload, delete or re-ingest documents")
require_ask = _require(ASK_ROLES, "ask questions")
require_review = _require(REVIEW_ROLES, "run extraction or review extracted values")
require_evals = _require(EVAL_ROLES, "run evaluations")
require_usage = _require(USAGE_ROLES, "see usage and the budget")
require_director = _require({Role.DIRECTOR}, "do that")


def permissions_for(user: User) -> dict[str, bool]:
    """What this person may do, resolved server-side.

    The frontend uses it to decide what to render; the API enforces the same
    sets again on every request regardless.
    """
    return {
        "can_upload": user.role in {r.value for r in UPLOAD_ROLES},
        "can_ask": user.role in {r.value for r in ASK_ROLES},
        "can_review": user.role in {r.value for r in REVIEW_ROLES},
        "can_run_evals": user.role in {r.value for r in EVAL_ROLES},
        "can_see_usage": user.role in {r.value for r in USAGE_ROLES},
    }


def visible_portfolio_ids(db: Session, user: User) -> list[uuid.UUID]:
    """The portfolios this person may see. Director: all; otherwise membership.

    Every retrieval query filters on this list, so an empty list means an
    empty result -- never an unfiltered one.
    """
    if user.role == Role.DIRECTOR:
        return list(db.execute(select(Portfolio.id).order_by(Portfolio.name)).scalars())
    return list(
        db.execute(
            select(PortfolioMember.portfolio_id).where(PortfolioMember.user_id == user.id)
        ).scalars()
    )


def get_visible_document(db: Session, user: User, document_id: uuid.UUID) -> Document:
    """Load a document the caller may see, or 404.

    The same 404 whether the document does not exist or exists in a portfolio
    the caller is not a member of. Distinguishing the two would tell a
    manager that there is a lease they are not allowed to know about.
    """
    document = db.get(Document, document_id)
    if document is None or document.portfolio_id not in set(visible_portfolio_ids(db, user)):
        raise NotFound("No such document.")
    return document
