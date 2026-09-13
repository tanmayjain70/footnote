# Working on Footnote

Orientation for anyone touching the code. What the project is, and why it is
built the way it is, is in [README.md](README.md) and [docs/](docs/); this file
is the practical half.

## Layout

```
api/            FastAPI + SQLAlchemy 2 + Alembic, Python 3.11+
  app/core/     settings, engine, one error shape, JWT
  app/models/   the schema (pgvector column on chunks)
  app/providers embeddings (fastembed | hashed) and answering (anthropic | stub)
  app/services/ pdf → chunking → ingestion; retrieval; answering; extraction; evals; usage
  app/routers/  thin HTTP layer
  app/demo/     the lease generator and the seed
  tests/        pytest against a real PostgreSQL with pgvector
web/            React 19 + TypeScript + Vite + TanStack Query + Tailwind 4
scripts/        database bootstrap, the user-space PostgreSQL helper, smoke test
docs/           brief, transcript, architecture, deployment, STATE
samples/        three generated leases, committed on purpose
```

## The database

PostgreSQL 17 **with pgvector**. Two roles: `footnote_owner` runs migrations,
`footnote_app` serves requests and owns nothing. The API refuses to start as a
superuser or as a table owner.

- Have PostgreSQL with pgvector on 5432: `scripts\bootstrap-db.ps1`.
- On Windows without admin rights (the usual case): `scripts\dev-db.ps1 install`
  builds a self-contained PostgreSQL 17 + pgvector under `%USERPROFILE%\tools`
  on port 5433 and bootstraps it. `dev-db.ps1 start|stop|status` afterwards; it
  is not a service and does not survive a reboot on its own.

## Commands

All from `api\` with the venv (`python -m venv .venv`, then
`.venv\Scripts\pip install -r requirements-dev.txt`):

```
.venv\Scripts\alembic upgrade head          # as footnote_owner (DATABASE_ADMIN_URL)
.venv\Scripts\python -m app.demo.seed       # 48 leases, users, golden set, extraction, one eval run
.venv\Scripts\uvicorn app.main:app --reload
.venv\Scripts\pytest -q                     # needs footnote_test; hashed embeddings, stub answers
.venv\Scripts\ruff check .
```

`web\`: `npm install`, `npm run dev` (proxies `/api` to 127.0.0.1:8000),
`npm run typecheck`, `npm run build`.

End to end against a running seeded server: `scripts\smoke-test.ps1`.

## House rules

- **Tests run against a real PostgreSQL with pgvector**, never SQLite or mocks
  of the database. Each test truncates as the owner; the runtime role cannot.
- **The stub providers are the test providers.** `EMBEDDING_PROVIDER=hashed`
  and `LLM_PROVIDER=stub` make the suite hermetic and deterministic. Anything
  that depends on the real model's judgement is measured by the evaluation
  harness, not asserted in a unit test.
- **Models and migrations must agree.** CI runs `alembic revision
  --autogenerate` and fails if it would produce anything. Change a model, write
  the migration, and check the downgrade.
- **One sentence splitter.** `app/services/chunking.split_sentences` is used at
  ingest, when building the blocks the model may cite, and when showing a
  chunk. A citation's block index means nothing if two splitters disagree.
- **Portfolio access lives in the retrieval query**, not in handlers. A document
  outside the caller's portfolios is a 404, never a 403.
- **Every citation is verified** against the passages the model was shown
  before it is stored; an answer with none is `unanswered`, whatever the prose
  says.
- Commits are authored by Tanmay Jain and by nobody else; no generated
  attribution lines. Conventional commit prefixes (`feat(api):`, `fix(web):`,
  `docs:`, `test:`, `chore:`).
- British English in prose and UI copy. Comments say why, not what.
- Keep `docs/STATE.md` current in the same commit as the work it describes.
