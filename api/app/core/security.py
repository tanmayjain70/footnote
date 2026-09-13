"""Password hashing and JWT issue/verify."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import get_settings

TokenKind = Literal["access", "refresh"]

# bcrypt silently truncates at 72 bytes; rejecting longer input is clearer than
# quietly ignoring the tail of somebody's passphrase.
MAX_PASSWORD_BYTES = 72


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("Password must be at most 72 bytes.")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


#: A real bcrypt digest of a password nobody has. Verifying against it costs
#: the same quarter-second as verifying a genuine one, which is the point:
#: the login route runs it when no account matches, so the response time does
#: not say whether an email address exists. A syntactically invalid salt --
#: the obvious way to write this -- raises immediately inside bcrypt and
#: leaves the timing difference it was meant to hide.
DUMMY_HASH = "$2b$12$xYM/iJBgbe2T1fLi8Yp9F.9KbCGIVNs1d1pmEWTdUCtDloJcbv5w."


def verify_password(plain: str, hashed: str) -> bool:
    raw = plain.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(subject: uuid.UUID, kind: TokenKind, extra: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=settings.access_token_minutes)
        if kind == "access"
        else timedelta(days=settings.refresh_token_days)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, expect: TokenKind) -> dict[str, Any]:
    """Decode and validate. Raises jwt.InvalidTokenError on anything wrong.

    The ``typ`` check matters: without it a refresh token, which lives for two
    weeks, would be accepted as an access token.
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("typ") != expect:
        raise jwt.InvalidTokenError(f"Expected a {expect} token.")
    return payload
