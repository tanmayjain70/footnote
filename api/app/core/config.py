"""Application settings.

Everything the application needs to know that is not code lives here, is typed,
and fails loudly at startup rather than at the first request that touches it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLM_PROVIDERS = {"stub", "anthropic"}
EMBEDDING_PROVIDERS = {"hashed", "fastembed"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = "development"

    database_url: str
    database_admin_url: str | None = None

    jwt_secret: str
    access_token_minutes: int = 30
    refresh_token_days: int = 14

    # --- language model ---------------------------------------------------
    #: ``stub`` is deterministic and free: it answers by quoting the best
    #: matching sentence, which is enough to exercise every citation check
    #: without a network. ``anthropic`` is the real thing.
    llm_provider: str = "stub"
    llm_model: str = "claude-opus-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 2048

    #: The SDK reads ANTHROPIC_API_KEY on its own. The setting exists so the
    #: health screen can say whether a key is configured without the app
    #: ever printing it.
    anthropic_api_key: str = ""
    #: Tests point this at a fake server; leave it unset otherwise.
    anthropic_base_url: str | None = None

    # --- embeddings -------------------------------------------------------
    #: ``hashed`` is feature hashing: no model, no download, instant, and
    #: good enough for the tests. The demo uses ``fastembed`` (bge-small),
    #: which is what retrieval quality is measured against.
    embedding_provider: str = "hashed"
    fastembed_cache_dir: str = ".fastembed_cache"
    fastembed_threads: int = 2

    # --- retrieval --------------------------------------------------------
    #: How many passages the model is shown, how many candidates each leg
    #: (vector, lexical) contributes before fusion, and the reciprocal-rank
    #: fusion constant. Recorded on every question so a change here is
    #: visible in the evals, not just felt.
    retrieval_k: int = 8
    retrieval_candidates: int = 30
    retrieval_rrf_k: int = 60
    chunk_target_chars: int = 1100

    # --- background work --------------------------------------------------
    #: The in-process worker that parses, chunks and embeds uploads. Tests
    #: switch it off and drain the queue synchronously instead.
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0

    max_upload_mb: int = 25
    #: When true the container seeds the demo data on start.
    demo_mode: bool = False

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    sql_echo: bool = False

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, v: str) -> str:
        if v in {"change-me", ""}:
            raise ValueError(
                "JWT_SECRET is still the placeholder. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return v

    @field_validator("llm_provider")
    @classmethod
    def _known_llm_provider(cls, v: str) -> str:
        if v not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(LLM_PROVIDERS)}, not {v!r}.")
        return v

    @field_validator("embedding_provider")
    @classmethod
    def _known_embedding_provider(cls, v: str) -> str:
        if v not in EMBEDDING_PROVIDERS:
            raise ValueError(
                f"EMBEDDING_PROVIDER must be one of {sorted(EMBEDDING_PROVIDERS)}, not {v!r}."
            )
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v: object) -> object:
        """Accept a JSON array or a plain comma-separated list."""
        if not isinstance(v, str):
            return v
        text = v.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [origin.strip() for origin in text.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
