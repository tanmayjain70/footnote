"""Engine, session factory, and the startup check that the runtime role is not
the table owner.

The application connects as ``footnote_app``, a role with DML rights and
nothing else. That is not decoration: a worker that writes thousands of chunk
rows per upload is exactly the code you do not want holding ``DROP TABLE``.
The check below runs at startup so a misconfigured deployment fails immediately
and visibly, rather than the first time somebody writes a bad migration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    echo=_settings.sql_echo,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def assert_runtime_role_is_least_privilege() -> None:
    """Fail startup if the API is connected as a superuser or a table owner.

    Two separate things are checked because they fail differently. A superuser
    ignores every grant in the database. A table owner passes the superuser
    check but can still ``ALTER`` and ``DROP`` the tables it owns.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT current_user AS role_name,
                       rolsuper      AS is_superuser
                  FROM pg_roles
                 WHERE rolname = current_user
                """
            )
        ).one()

        if row.is_superuser:
            raise RuntimeError(
                f"The API is connected as superuser '{row.role_name}'. Point "
                "DATABASE_URL at the low-privilege application role; migrations "
                "use DATABASE_ADMIN_URL."
            )

        owned = conn.execute(
            text(
                """
                SELECT count(*)
                  FROM pg_tables
                 WHERE schemaname = 'public'
                   AND tableowner = current_user
                """
            )
        ).scalar_one()

        if owned:
            raise RuntimeError(
                f"The API's role '{row.role_name}' owns {owned} table(s) in "
                "public. The runtime role must not be the table owner -- an "
                "owner can ALTER and DROP regardless of grants. Run "
                "scripts/bootstrap-db.ps1, which creates the two roles."
            )

        logger.info("runtime role %s verified: not superuser, owns no tables", row.role_name)
