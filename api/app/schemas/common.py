from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class Ok(BaseModel):
    ok: bool = True
    message: str | None = None


class PortfolioOut(ORMModel):
    """A portfolio as the caller sees it, with how much of it is searchable."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    confidential: bool
    document_count: int
    #: Documents that have been parsed, chunked and embedded. The difference
    #: from ``document_count`` is what the worker has not reached yet.
    ready_count: int
