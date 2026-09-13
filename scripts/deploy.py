"""Provision the demo deployment: a Neon project, then two Render services.

Reads two API keys from the environment and does the whole thing:

    NEON_API_KEY=...  RENDER_API_KEY=...  python scripts/deploy.py

Idempotent by name. If a Neon project called ``footnote`` or a Render service
called ``footnote-api`` already exists it is reused rather than duplicated, so
a failed half-run can simply be run again.

The two API keys come from the environment. The connection strings it
generates are written to ``api/.env.production.local``, which is gitignored --
they are needed again if a service is ever rebuilt by hand, and nobody
remembers a generated password.

The repository is private, so Render's GitHub app has to have been granted
access to it. If it has not, service creation fails with a message about the
repository not being found; the fix is one click in Render's dashboard under
Settings -> GitHub, and then this script runs again.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any

REPO = "https://github.com/tanmayjain70/footnote"
BRANCH = "main"
PROJECT = "footnote"
APP_ROLE = "footnote_app"
REGION_NEON = "aws-us-east-2"
REGION_RENDER = "ohio"  # same continent as the database; cross-region adds latency to every query

NEON_API = "https://console.neon.tech/api/v2"
RENDER_API = "https://api.render.com/v1"

#: Everything the API service needs that is not a secret or a URL decided
#: later. Embeddings run in the container, so nothing about a document leaves
#: it to be indexed; one thread keeps the model inside a free instance's
#: memory. Answers are extractive until somebody sets a key, which is the
#: honest default for a public demo with a spending limit.
STATIC_ENV = {
    "ENVIRONMENT": "production",
    "DEMO_MODE": "true",
    "LLM_PROVIDER": "stub",
    "LLM_MODEL": "claude-opus-5",
    "EMBEDDING_PROVIDER": "fastembed",
    "FASTEMBED_CACHE_DIR": "/app/.fastembed_cache",
    "FASTEMBED_THREADS": "1",
    "WORKER_ENABLED": "true",
}


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}\n{body[:800]}")
        self.status = status
        self.body = body


def call(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(method=method, url=url, data=data)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        raise ApiError(method, url, exc.code, exc.read().decode()) from None


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def detail(text: str) -> None:
    print(f"   {text}", flush=True)


# ------------------------------------------------------------------- neon ---


def neon_org(token: str) -> str:
    """Neon requires an org id on every project call; accounts have exactly one."""
    orgs = call("GET", f"{NEON_API}/users/me/organizations", token)["organizations"]
    if not orgs:
        raise SystemExit("No Neon organization found for this API key.")
    detail(f"Neon org {orgs[0]['name']} ({orgs[0]['id']}, {orgs[0]['plan']})")
    return orgs[0]["id"]


def neon_project(token: str, org_id: str) -> tuple[str, str]:
    """Return (project_id, owner_connection_uri), creating the project if needed."""
    existing = call("GET", f"{NEON_API}/projects?org_id={org_id}", token)
    for project in existing.get("projects", []):
        if project["name"] == PROJECT:
            detail(f"reusing existing Neon project {project['id']}")
            uris = call(
                "GET",
                f"{NEON_API}/projects/{project['id']}/connection_uri"
                f"?database_name=neondb&role_name={project.get('owner_name', 'neondb_owner')}",
                token,
            )
            return project["id"], uris["uri"]

    created = call(
        "POST",
        f"{NEON_API}/projects",
        token,
        {
            "project": {
                "name": PROJECT,
                "pg_version": 17,
                "region_id": REGION_NEON,
                "org_id": org_id,
            }
        },
    )
    project_id = created["project"]["id"]
    uri = created["connection_uris"][0]["connection_uri"]
    detail(f"created Neon project {project_id} ({REGION_NEON}, PostgreSQL 17)")
    return project_id, uri


def bootstrap_database(owner_uri: str, app_password: str) -> str:
    """Create the vector extension and the low-privilege runtime role.

    Returns the runtime role's connection URI. Neither half is optional: the
    schema does not exist without pgvector, and the API asserts at startup
    that it is not a superuser and owns no tables, so pointing it at the owner
    role would start a service that immediately refuses to serve.
    """
    import psycopg

    with psycopg.connect(owner_uri, autocommit=True) as conn:
        database = conn.info.dbname
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        installed = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if installed is None:
            raise SystemExit("pgvector is not available on this Neon project.")
        detail(f"pgvector {installed[0]} ready in {database}")

        conn.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{app_password}'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE;
                ELSE
                    ALTER ROLE {APP_ROLE} PASSWORD '{app_password}';
                END IF;
            END
            $$;
            """
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {APP_ROLE}')
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

        privileged = conn.execute(
            f"SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = '{APP_ROLE}'"
        ).fetchone()[0]
        if privileged:
            raise SystemExit(f"{APP_ROLE} is privileged; refusing to continue.")

    detail(f"{APP_ROLE} created and verified unprivileged in {database}")

    # Swap the role and password into the owner URI, and switch the scheme to
    # the one SQLAlchemy needs. Neon hands out postgresql://; psycopg is the
    # driver, and without naming it SQLAlchemy reaches for psycopg2.
    tail = owner_uri.split("@", 1)[1]
    return f"postgresql+psycopg://{APP_ROLE}:{app_password}@{tail}"


def as_sqlalchemy(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# ----------------------------------------------------------------- render ---


def render_owner(token: str) -> str:
    owners = call("GET", f"{RENDER_API}/owners?limit=20", token)
    owner_id = owners[0]["owner"]["id"]
    detail(f"Render owner {owners[0]['owner']['name']} ({owner_id})")
    return owner_id


def find_service(token: str, name: str) -> dict[str, Any] | None:
    services = call("GET", f"{RENDER_API}/services?name={name}&limit=20", token)
    for entry in services:
        if entry["service"]["name"] == name:
            return entry["service"]
    return None


def env_vars(pairs: dict[str, str]) -> list[dict[str, str]]:
    return [{"key": k, "value": v} for k, v in pairs.items()]


def api_env(database_url: str, admin_url: str, jwt_secret: str, cors: str | None) -> dict[str, str]:
    values = dict(STATIC_ENV)
    values.update(
        {
            "DATABASE_URL": database_url,
            "DATABASE_ADMIN_URL": admin_url,
            "JWT_SECRET": jwt_secret,
        }
    )
    if cors:
        values["CORS_ORIGINS"] = cors
    return values


def create_api_service(
    token: str, owner_id: str, database_url: str, admin_url: str, jwt_secret: str
) -> dict[str, Any]:
    existing = find_service(token, "footnote-api")
    if existing:
        detail("reusing existing footnote-api")
        call(
            "PUT",
            f"{RENDER_API}/services/{existing['id']}/env-vars",
            token,
            env_vars(api_env(database_url, admin_url, jwt_secret, None)),
        )
        return existing

    created = call(
        "POST",
        f"{RENDER_API}/services",
        token,
        {
            "type": "web_service",
            "name": "footnote-api",
            "ownerId": owner_id,
            "repo": REPO,
            "branch": BRANCH,
            "autoDeploy": "yes",
            "rootDir": "api",
            "serviceDetails": {
                "env": "docker",
                "region": REGION_RENDER,
                "plan": "free",
                # Liveness only: /healthz does not touch Postgres, so a
                # database blip cannot make Render tear down a healthy
                # container while the seeder is still filling it.
                "healthCheckPath": "/api/v1/healthz",
                "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."},
            },
            "envVars": env_vars(api_env(database_url, admin_url, jwt_secret, None)),
        },
    )
    service = created["service"]
    detail(f"created footnote-api ({service['id']})")
    return service


def create_web_service(token: str, owner_id: str, api_base: str) -> dict[str, Any]:
    existing = find_service(token, "footnote-web")
    if existing:
        detail("reusing existing footnote-web")
        call(
            "PUT",
            f"{RENDER_API}/services/{existing['id']}/env-vars",
            token,
            env_vars({"VITE_API_BASE": api_base}),
        )
        # Vite reads this at build time and writes the value into the bundle,
        # so changing the variable changes nothing until something rebuilds.
        # A service that already existed has a bundle pointing at whatever the
        # API's address was last time.
        call(
            "POST",
            f"{RENDER_API}/services/{existing['id']}/deploys",
            token,
            {"clearCache": "do_not_clear"},
        )
        detail("rebuilding it so the new API address is in the bundle")
        return existing

    created = call(
        "POST",
        f"{RENDER_API}/services",
        token,
        {
            "type": "static_site",
            "name": "footnote-web",
            "ownerId": owner_id,
            "repo": REPO,
            "branch": BRANCH,
            "autoDeploy": "yes",
            "rootDir": "web",
            "serviceDetails": {
                "buildCommand": "npm ci && npm run build",
                "publishPath": "./dist",
                # Single-page app: every unmatched path must return index.html
                # or a browser refresh on /register is a 404 from the static
                # host.
                "routes": [{"type": "rewrite", "source": "/*", "destination": "/index.html"}],
            },
            "envVars": env_vars({"VITE_API_BASE": api_base}),
        },
    )
    service = created["service"]
    detail(f"created footnote-web ({service['id']})")
    return service


def wait_for(token: str, service_id: str, name: str, minutes: int = 25) -> str:
    """Poll the latest deploy until it settles.

    The API image bakes in the embedding model, so its first build is long;
    the seeding that follows happens behind the server and does not hold the
    deploy up.
    """
    deadline = time.time() + minutes * 60
    last = ""
    while time.time() < deadline:
        deploys = call("GET", f"{RENDER_API}/services/{service_id}/deploys?limit=1", token)
        if not deploys:
            time.sleep(10)
            continue
        status = deploys[0]["deploy"]["status"]
        if status != last:
            detail(f"{name}: {status}")
            last = status
        if status in {"live", "build_failed", "update_failed", "canceled", "deactivated"}:
            return status
        time.sleep(15)
    return last or "timed_out"


def main() -> int:
    neon_key = os.environ.get("NEON_API_KEY")
    render_key = os.environ.get("RENDER_API_KEY")
    if not neon_key or not render_key:
        print("Set NEON_API_KEY and RENDER_API_KEY in the environment.", file=sys.stderr)
        return 2

    app_password = secrets.token_urlsafe(24)
    jwt_secret = secrets.token_urlsafe(48)

    step("Neon")
    org_id = neon_org(neon_key)
    _, owner_uri = neon_project(neon_key, org_id)

    step("The extension and the runtime role")
    database_url = bootstrap_database(owner_uri, app_password)
    admin_url = as_sqlalchemy(owner_uri)

    step("Render")
    owner_id = render_owner(render_key)
    api = create_api_service(render_key, owner_id, database_url, admin_url, jwt_secret)
    api_host = api.get("serviceDetails", {}).get("url") or f"https://{api['name']}.onrender.com"
    web = create_web_service(render_key, owner_id, f"{api_host}/api/v1")
    web_host = web.get("serviceDetails", {}).get("url") or f"https://{web['name']}.onrender.com"

    step("CORS")
    call(
        "PUT",
        f"{RENDER_API}/services/{api['id']}/env-vars",
        render_key,
        env_vars(api_env(database_url, admin_url, jwt_secret, web_host)),
    )
    detail(f"allowed origin {web_host}")

    # Render snapshots environment variables into a deploy. The API service was
    # created before the frontend existed, so its first deploy has no
    # CORS_ORIGINS -- and a *restart* does not help, because it replays the
    # same snapshot. Only a new deploy picks the value up. Without this the
    # services come up green and the browser is refused at the door.
    step("Redeploying the API so it picks up CORS_ORIGINS")
    call(
        "POST",
        f"{RENDER_API}/services/{api['id']}/deploys",
        render_key,
        {"clearCache": "do_not_clear"},
    )

    step("Waiting for the deploys")
    api_status = wait_for(render_key, api["id"], "api")
    web_status = wait_for(render_key, web["id"], "web")

    record = pathlib.Path("api/.env.production.local")
    record.write_text(
        "\n".join(
            [
                "# Written by scripts/deploy.py. Gitignored.",
                "# The live deployment's connection strings, kept because the",
                "# app-role password is generated and is recoverable from",
                "# nowhere else.",
                f"DATABASE_URL={database_url}",
                f"DATABASE_ADMIN_URL={admin_url}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    step("Done")
    print(f"   API          {api_host}   [{api_status}]")
    print(f"   Web          {web_host}   [{web_status}]")
    print(f"   credentials  {record}  (gitignored)")
    print()
    print("   The API seeds itself behind the server: 48 leases to parse and")
    print("   embed, a few minutes. Sign in at the web address as")
    print("   director@hallampryce.demo / demo-password.")
    return 0 if api_status == "live" else 1


if __name__ == "__main__":
    sys.exit(main())
