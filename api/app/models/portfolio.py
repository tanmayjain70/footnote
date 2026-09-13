"""Portfolios and who may see them.

A portfolio is the unit of access control. The brief's motivating case is a
portfolio being sold: confidential to the director and one manager, invisible
to everybody else -- not "visible but read-only", invisible. Membership is a
plain join table so that the retrieval query can filter on it with
``portfolio_id = ANY(:visible)`` and nothing downstream has to remember to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk


class Portfolio(Base, TimestampMixin):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Informational. Access is decided by membership either way; the flag is
    #: so the UI can say why a portfolio has two members instead of eight.
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    members: Mapped[list[PortfolioMember]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan", passive_deletes=True
    )


class PortfolioMember(Base):
    __tablename__ = "portfolio_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="members")
