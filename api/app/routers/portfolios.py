from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.deps import current_user, visible_portfolio_ids
from app.models import Document, Portfolio, User
from app.models.enums import DocumentStatus
from app.schemas.common import PortfolioOut

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


@router.get("", response_model=list[PortfolioOut])
def list_portfolios(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[PortfolioOut]:
    """The portfolios the caller can see, and how much of each is searchable."""
    visible = visible_portfolio_ids(db, user)
    if not visible:
        return []

    counts = {
        portfolio_id: (total, ready)
        for portfolio_id, total, ready in db.execute(
            select(
                Document.portfolio_id,
                func.count(),
                func.count().filter(Document.status == DocumentStatus.READY),
            )
            .where(Document.portfolio_id.in_(visible))
            .group_by(Document.portfolio_id)
        ).all()
    }
    portfolios = db.execute(
        select(Portfolio).where(Portfolio.id.in_(visible)).order_by(Portfolio.name)
    ).scalars()

    return [
        PortfolioOut(
            id=p.id,
            name=p.name,
            slug=p.slug,
            description=p.description,
            confidential=p.confidential,
            document_count=counts.get(p.id, (0, 0))[0],
            ready_count=counts.get(p.id, (0, 0))[1],
        )
        for p in portfolios
    ]
