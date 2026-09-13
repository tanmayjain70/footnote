"""The register: key lease terms across every lease the caller can see.

This is the answer to "which leases have a break in 2027" -- a table, not a
chat. Values that nobody has confirmed are left out unless asked for, and the
CSV export applies exactly the same rule, because the export is what ends up
in front of a landlord.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFound
from app.deps import current_user, visible_portfolio_ids
from app.models import User
from app.schemas.extraction import RegisterCounts, RegisterResponse, RegisterRow
from app.services import extraction as extraction_service

router = APIRouter(prefix="/register", tags=["register"])


def _rows(
    db: Session,
    user: User,
    *,
    portfolio_id: uuid.UUID | None,
    expiring_before: date | None,
    has_break: bool | None,
    include_unreviewed: bool,
) -> list[dict]:
    visible = visible_portfolio_ids(db, user)
    if portfolio_id is not None and portfolio_id not in set(visible):
        raise NotFound("No such portfolio.")
    return extraction_service.register_rows(
        db,
        visible_ids=visible,
        portfolio_id=portfolio_id,
        expiring_before=expiring_before,
        has_break=has_break,
        include_unreviewed=include_unreviewed,
    )


@router.get("", response_model=RegisterResponse)
def get_register(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    portfolio_id: uuid.UUID | None = None,
    expiring_before: date | None = Query(default=None, description="Term ends before this day."),
    has_break: bool | None = Query(
        default=None, description="Only leases with (or without) a break."
    ),
    include_unreviewed: bool = Query(
        default=False, description="Show pending values too. They are marked, and off by default."
    ),
) -> RegisterResponse:
    rows = _rows(
        db,
        user,
        portfolio_id=portfolio_id,
        expiring_before=expiring_before,
        has_break=has_break,
        include_unreviewed=include_unreviewed,
    )
    return RegisterResponse(
        rows=[RegisterRow(**row) for row in rows],
        counts=RegisterCounts(**extraction_service.register_counts(rows)),
    )


@router.get("/export.csv", response_class=Response)
def export_register(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    portfolio_id: uuid.UUID | None = None,
    expiring_before: date | None = None,
    has_break: bool | None = None,
    include_unreviewed: bool = False,
) -> Response:
    """The same rows as ``GET /register``, as a CSV attachment."""
    rows = _rows(
        db,
        user,
        portfolio_id=portfolio_id,
        expiring_before=expiring_before,
        has_break=has_break,
        include_unreviewed=include_unreviewed,
    )
    filename = f"register-{date.today().isoformat()}.csv"
    return Response(
        content=extraction_service.register_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
