"""Footnote API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal, assert_runtime_role_is_least_privilege
from app.core.errors import install_error_handlers
from app.routers import (
    ask,
    auth,
    documents,
    evals,
    extraction,
    health,
    portfolios,
    register,
    usage,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
)
logger = logging.getLogger("footnote")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    assert_runtime_role_is_least_privilege()

    # The worker parses, chunks and embeds uploads in a background thread of
    # this process. Guarded on import because the service is built after the
    # foundation it runs on; the API must start regardless.
    worker_module = None
    if settings.worker_enabled:
        try:
            from app.services import ingestion as worker_module
        except ImportError:
            logger.warning("ingestion service not available; uploads will queue and wait")
        else:
            worker_module.start_worker()
            logger.info("ingestion worker started")

    yield

    if worker_module is not None:
        with suppress(Exception):
            worker_module.stop_worker()


app = FastAPI(
    title="Footnote",
    version="0.1.0",
    description=(
        "Cited answers over a property firm's leases, and an honest refusal when "
        "the evidence is not there. Hybrid retrieval over PDF leases, answers whose "
        "every citation is checked against the passages the model was shown, a "
        "reviewed register of key terms, and an evaluation harness."
    ),
    lifespan=lifespan,
    docs_url="/docs",
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(portfolios.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(ask.router, prefix=API_PREFIX)
app.include_router(extraction.router, prefix=API_PREFIX)
app.include_router(register.router, prefix=API_PREFIX)
app.include_router(evals.router, prefix=API_PREFIX)
app.include_router(usage.router, prefix=API_PREFIX)
app.include_router(health.router, prefix=API_PREFIX)


@app.get(f"{API_PREFIX}/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Liveness. Deliberately does not touch the database."""
    return {"status": "ok"}


@app.get(f"{API_PREFIX}/readyz", include_in_schema=False)
def readyz() -> dict[str, str]:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    finally:
        db.close()


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "footnote", "docs": "/docs", "health": f"{API_PREFIX}/healthz"}
