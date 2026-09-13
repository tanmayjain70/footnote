"""The health screen and the queue drain.

The screen answers "can I trust this right now": is the worker alive, what is
stuck, what is the model, and how much of today's budget is gone. Every figure
is read from the tables directly so the endpoint keeps answering when a service
module is missing or broken -- the moment you most want a health screen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ServiceUnavailable
from app.deps import current_user, require_director
from app.models import EMBEDDING_DIMENSIONS, Document, Job, OrgSettings, UsageEvent, User
from app.models.enums import USAGE_ROLES, DocumentStatus, JobStatus

router = APIRouter(tags=["health"])


def _worker_alive() -> bool:
    try:
        from app.services import ingestion
    except ImportError:
        return False
    worker = getattr(ingestion, "worker", None)
    return bool(worker is not None and getattr(worker, "alive", False))


def _llm_info(settings: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "api_key_configured": bool(settings.anthropic_api_key),
    }
    try:
        from app.providers import llm

        provider = llm.get_llm_provider()
        info["provider"] = provider.name
        info["model"] = provider.model
    except Exception:  # a broken provider must not take the health screen down
        pass
    return info


def _embeddings_info(settings: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "provider": settings.embedding_provider,
        "model": None,
        "dimensions": EMBEDDING_DIMENSIONS,
    }
    try:
        from app.providers import embeddings

        provider = embeddings.get_embedding_provider()
        info["provider"] = provider.name
        info["model"] = provider.model
        info["dimensions"] = provider.dimensions
    except Exception:  # as above
        pass
    return info


@router.get("/health")
def health(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    settings = get_settings()

    jobs = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    documents = dict(
        db.execute(select(Document.status, func.count()).group_by(Document.status)).all()
    )

    # Spend is measured against the UTC day because that is when the budget
    # resets; a local-time day would give a Manchester user a different limit
    # in summer and winter.
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = db.execute(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.created_at >= day_start
        )
    ).scalar_one()
    org = db.get(OrgSettings, 1)
    budget = org.daily_budget_usd if org else Decimal("0")

    return {
        "worker_alive": _worker_alive(),
        "jobs": {
            "queued": jobs.get(JobStatus.QUEUED, 0),
            "running": jobs.get(JobStatus.RUNNING, 0),
            "failed": jobs.get(JobStatus.FAILED, 0),
        },
        "documents": {
            "ready": documents.get(DocumentStatus.READY, 0),
            "processing": documents.get(DocumentStatus.PROCESSING, 0),
            "failed": documents.get(DocumentStatus.FAILED, 0),
        },
        "llm": _llm_info(settings),
        "embeddings": _embeddings_info(settings),
        # What the firm spends is the director's business. The header reads
        # the job counts and the model name from here; the budget block is
        # for the one role that may see the Usage screen at all.
        "budget": (
            {
                "daily_budget_usd": str(Decimal(budget).quantize(Decimal("0.01"))),
                "spent_today_usd": str(Decimal(spent).quantize(Decimal("0.000001"))),
            }
            if user.role in {r.value for r in USAGE_ROLES}
            else None
        ),
    }


@router.post("/jobs/drain")
def drain_jobs(
    db: Session = Depends(get_db), _: User = Depends(require_director)
) -> dict[str, int]:
    """Run every queued job now, in this request. For demos and for the day
    the worker thread is not running; the worker does the same thing on its
    own schedule."""
    try:
        from app.services import ingestion
    except ImportError:
        raise ServiceUnavailable("The ingestion service is not available.") from None
    return {"processed": ingestion.process_pending(db)}
