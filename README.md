# Footnote

Cited answers over a property firm's leases — and an honest refusal when the
evidence is not there. Hybrid retrieval in PostgreSQL, answers whose every
citation is checked against the passage the model was shown, a register of
key lease terms that a person confirms before it counts, and an evaluation
harness that measures whether any of it is getting worse.
**React · Python · PostgreSQL + pgvector**

[![CI](https://github.com/tanmayjain70/footnote/actions/workflows/ci.yml/badge.svg)](https://github.com/tanmayjain70/footnote/actions/workflows/ci.yml)

> **Milestone 1 of 5.** Ingest, cited answers with verified citations,
> portfolio-level confidentiality, the reviewed register, the daily budget and
> the evaluation harness. OCR for scanned leases, conversation memory,
> Word/Excel ingestion, expiry reminders and single sign-on are deferred — each
> waits on something the client owes, set out in
> [docs/brief.md](docs/brief.md#8-milestones).

---

## The problem

A commercial property management firm in Manchester: 380 units, about 1,400
leases and side letters as PDFs, four property managers and two lease
administrators who answer questions out of them all day.

> "A contractor set us up with one of those chat-with-your-PDFs tools. It told
> a lease administrator the break notice was three months. It was six. We
> served notice late and the tenant is now locked in for another five years.
> **I do not need it to be clever. I need it to point at the page, and I need
> it to say 'I don't know' when it doesn't.**"

That sentence is the whole product. Two more came out of the scoping
questions: *"what stops this costing us £2,000 in a month?"* and *"how will I
know it is not getting worse?"* — which is why there is a budget and an
evaluation harness rather than a chatbot.

---

## What it does

In plain terms, for anyone who would rather not read code.

**It reads the leases once.** Drop in a PDF; it is split into pages and
sentences, indexed two ways, and ready in seconds. Upload the same file twice
and nothing happens — it recognises the bytes. A scanned lease with no text is
refused with a message that says so, rather than quietly indexed as nothing.

**It answers from the pages, and shows you which.** Ask *"when does the term at
Unit 4, Meridian House expire?"* and the answer arrives word by word with a
small numbered marker after each sentence. Click the marker and the passage it
came from is on the right, with a link that opens the page and highlights the
sentence. Every marker was **checked against the passages the model was
actually given** before it was shown; a citation that points anywhere else is
dropped and counted.

**It says when it cannot answer.** A question the leases do not cover comes
back as *"not answerable from the documents you can see"*, with the closest
passages alongside so a person can judge. An answer with nothing to point at is
never presented as an answer, whatever the prose says.

**It builds a register a person has signed off.** Seventeen key terms per lease
— parties, term, rent, review, break, notice, repair, use, deposit, guarantor,
VAT — extracted with a quote and a page for each, then confirmed, corrected or
rejected by a lease administrator. The register and its export show confirmed
values only; unreviewed ones are visible on request, and marked.

**It keeps one portfolio's leases away from the wrong eyes.** Each person sees
the portfolios they belong to. The portfolio being marketed for sale is visible
to two people, and a document outside your portfolios is a 404 — not a 403,
which would confirm it exists.

**It meters every model call and stops at the daily limit.** Tokens and cost
are recorded per question; the budget is checked before a call, never after,
and a question over the line gets a clear refusal rather than a bill.

**And it measures itself.** A golden set of questions with known answers — and
a dozen with none — runs against retrieval in seconds or end to end for money,
and reports hit rates, refusals and cost per run so a change to chunking or the
model is a number, not a feeling.

---

## What the demo shows

The demo corpus is generated, so the truth is known: 48 leases across three
portfolios, 524 pages, 1,728 passages, with 174 golden questions whose
expected page is recorded and 12 questions a lease never answers.

| | |
|---|---|
| **94%** | of the 174 answerable questions had the expected *page* among the eight passages retrieved |
| **100%** | had the expected *document* — mean reciprocal rank 0.69 |
| **7 of 12** | unanswerable questions were refused outright by the extractive stub; the harness names the five it answered, which is the point of having one |
| **7 of 186** | false answers end to end with the stub — a citation into the wrong document, or an unanswerable question answered — against 90% of answerable questions answered |
| **816** | extracted values across 48 leases, every one with a quote and a page; 24 leases fully confirmed, 2 with corrections, the rest waiting for review |

Those retrieval figures are with the local embedding model and no model
writing the answers: the demo's answers are extractive unless a model key is
configured, which is the honest default for a public demo with a spending
limit. The end-to-end run reports the same metrics for whichever answerer is
configured.

---

## Live demo

The demo deploys from [`render.yaml`](render.yaml) to Render's free tier with
the database on Neon, in about twenty minutes:
[docs/deployment.md](docs/deployment.md).

> Hosted on a free tier that sleeps when idle, so the **first sign-in can take
> up to a minute** while the server wakes. The login page says so while it
> waits.

Every account uses the password `demo-password`.

| Account | Role | What it shows |
|---|---|---|
| `director@hallampryce.demo` | Director | Everything: all three portfolios, evaluations, usage and the budget |
| `admin@hallampryce.demo` | Lease administrator | Uploads, extraction, the review queue |
| `manager@hallampryce.demo` | Property manager | Asks questions across City Centre and Northern Estates — **cannot see Riverside** |
| `riverside@hallampryce.demo` | Property manager | Riverside only, the portfolio being sold |
| `finance@hallampryce.demo` | Viewer | The register, read-only. **Cannot ask**: the API returns 403 even though the page exists |

### Three things worth trying

1. **Ask something the leases answer, then something they do not.** *"When
   does the term at Suite 1, Clippers Quay House expire?"* comes back cited.
   *"What are the landlord's bank account details for paying the rent?"*
   comes back as not answerable, with the passages it looked at — and no
   citation, because there is nothing to cite. (Then try *"What is the
   tenant's VAT registration number?"*: the extractive stub cites the VAT
   clause, which is precisely the kind of confident miss the evaluation
   harness exists to count.)
2. **Sign in as the manager and paste a Riverside document's id into the
   address bar.** You get a 404. Confirming the lease exists would leak that it
   does.
3. **Set the daily budget to zero on the Usage page, then ask a question.** A
   429 that names the spend and the limit, before a single token is sent. Put
   the budget back afterwards.

---

## The interesting part

The same story again, for engineers.

### A citation is a structured object, verified twice

Each retrieved passage is sent to the model as a document whose content is the
passage's sentences, with citations enabled. The answer comes back as text
blocks carrying citations that name a document index and a sentence range —
produced by the API, not parsed out of prose. The server then checks every one
against the passages it sent: wrong document index or a sentence range outside
the passage, and the citation is dropped and counted. **Zero surviving
citations means `unanswered`**, whatever the text says.

The stub answerer used by the tests and the keyless demo produces exactly the
same events, extractively, so the whole path — retrieval, verification,
storage, the streamed UI — is exercised without a key.

### One sentence splitter

A citation is a block index into a chunk. That only means something if the
sentences the model was shown are the sentences the page viewer highlights, so
one function splits text everywhere, and a chunk's text is its sentences
joined with newlines: splitting a chunk gives back exactly its sentences, and a
test asserts it.

### Which lease, then which passage

A question about a lease has two halves: *"the Eccles Insurance Brokers lease
at Lancastrian Business Park"* names a document, *"when does the term end"*
asks about a clause. Searched as one string, the cover page and the parties
clause win on every measure because they are dense with the names, and the
clause that answers is not among the eight passages shown to the model. The
first version of retrieval did exactly that: right document 93% of the time,
right page 33%.

So the rarer words of the question are matched against each visible document's
identity — title, metadata, the head of its first page — and when two or more
of them single out a lease (or a small tie), the search is confined to it and
the *remaining* words rank the passages. That took the right page from 33% to
94% on the golden set, measured by the harness in six seconds, which is what
the harness is for.

Within the scope, two legs: cosine distance over pgvector (HNSW) and full-text
rank over a stored generated `tsvector` (GIN), each contributing thirty
candidates, fused by reciprocal rank because a distance and a text rank are not
on the same scale. Leases are full of exact tokens — "Unit 4B", "£42,500" —
that an embedding blurs, and paraphrase that a lexical index misses. The
portfolio filter is inside the one search function, which takes the caller's
visible set as a required argument; there is no `WHERE` clause for a new
endpoint to forget.

### A queue in PostgreSQL

Ingest, extraction and evaluation runs are rows in `jobs`, claimed with
`FOR UPDATE SKIP LOCKED` by a worker thread in the API. No Celery, no Redis:
a second process is one more thing to run, and PostgreSQL already provides a
queue that is transactional with the rows the job writes. A test runs twenty
jobs through two threads and proves nothing is claimed twice.

### The register does not trust the model either

Every extracted value carries a quote and the chunk it came from. The server
checks that the chunk belongs to the document and that the quote is in it,
parses the value by type (day-first dates, always), and records the first
problem it finds. Nothing counts until a person confirms it. The client asked
whether unconfirmed values could "just count for now" — see
[docs/brief.md](docs/brief.md#7-what-the-brief-got-wrong) for why not.

### The budget is checked before the call

Every model call is a `usage_events` row with tokens and cost from a price
table. The daily limit is compared with today's sum before a request is sent;
over the line is a 429 that says how much was spent against what. The last
call of a day can overshoot by one call, which is documented rather than fixed
with a reservation scheme.

---

## How it is built

```
api/
  app/
    core/            settings, engine, one error shape, JWT
    models/          the schema; a pgvector column and a generated tsvector on chunks
    providers/       embeddings (fastembed | hashed) · answering (anthropic | stub) · prices
    services/
      pdf, chunking  text out of a PDF; one sentence splitter
      ingestion      the jobs table, SKIP LOCKED, the worker thread
      retrieval      two legs, reciprocal rank fusion, the ACL in the query
      answering      streaming, citation verification, the unanswered rule
      extraction     structured extraction, evidence checks, review, the register
      evals          golden set, retrieval and end-to-end runs
      usage          metering and the daily budget
    routers/         FastAPI, thin
    demo/            the lease generator and the seed
  alembic/           one migration, reversible
  tests/             323 tests against a real PostgreSQL with pgvector
web/
  src/pages/         ask · documents · document · register · evals · usage
samples/             three generated leases, committed on purpose
scripts/             database bootstrap, the user-space PostgreSQL helper, smoke test
docs/                the brief, the transcript, architecture, deployment, STATE
```

**Backend** FastAPI · SQLAlchemy 2 · Alembic · psycopg 3 · PostgreSQL 17 +
pgvector · pypdf · ReportLab · fastembed (bge-small, ONNX) · Anthropic SDK
**Frontend** React 19 · TypeScript · Vite · TanStack Query · Tailwind 4 · no
charting library
**Infra** Docker · GitHub Actions · Render + Neon

Deliberate omissions, each with a reason in
[docs/architecture.md](docs/architecture.md#8-what-was-deliberately-not-built):
no OCR, no conversation memory, no re-ranker, no vector database, no task
queue, no row-level security.

### Two Postgres roles

`footnote_owner` owns the tables and runs migrations. `footnote_app` has DML
and nothing else, and the API asserts at startup that it is neither a superuser
nor a table owner. CI creates both roles for the same reason: a
single-superuser test database would let the suite pass while the guarantee
quietly did not hold.

---

## Running it locally

Needs PostgreSQL 17 **with pgvector**, Python 3.11+, Node 20+.

On Windows without administrator rights — the usual case — the installer's
PostgreSQL has no pgvector and cannot be given one. One script builds a
self-contained PostgreSQL 17 + pgvector in your user profile, on port 5433,
with the Visual Studio Build Tools, and bootstraps the roles:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev-db.ps1 install
```

Against a PostgreSQL that already has the extension, on 5432:

```
set PGSUPERPASSWORD=your-postgres-password
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap-db.ps1
```

Then:

```
cd api
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env
rem  set JWT_SECRET; change 5432 to 5433 in both URLs if you used dev-db.ps1
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m app.demo.seed
.venv\Scripts\uvicorn app.main:app --reload

rem  second terminal
cd web
npm install
npm run dev                       rem  http://localhost:5173
```

The seed downloads the embedding model once (65 MB) and then embeds the corpus;
expect a few minutes the first time. Answers use the extractive stub unless
`LLM_PROVIDER=anthropic` and a key are set in `api\.env`.

```
cd api
.venv\Scripts\pytest               rem  323 tests against footnote_test, 96% coverage
.venv\Scripts\ruff check .
```

The tests use hashed embeddings and the stub answerer, so they need no model
download and no key. An end-to-end smoke test asserts on answers rather than
status codes — 28 checks covering the cited answer, the refusal,
the 404, the review, the register, an evaluation run and the budget:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke-test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke-test.ps1 -BaseUrl https://your-api-host
```

API docs at <http://127.0.0.1:8000/docs>.

---

## Documentation

| | |
|---|---|
| [docs/brief.md](docs/brief.md) | The client brief, and what changed after asking |
| [docs/brief-transcript.md](docs/brief-transcript.md) | The scoping exchange, in full: the job post, fourteen questions, the answers, the negotiation |
| [docs/architecture.md](docs/architecture.md) | How it works, and what was left out |
| [docs/deployment.md](docs/deployment.md) | Render + Neon, and the local setup |
| [docs/STATE.md](docs/STATE.md) | Where the project stands, and what is not verified |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Commands, layout and house rules |
| [samples/README.md](samples/README.md) | The three sample leases and what to ask about them |

---

## A note on what this is

**A simulated engagement.** Footnote is a personal project. There is no real
client, no money changed hands, and "Hallam & Pryce" is not a real company. The
brief was written as a realistic exercise in scoping a document-intelligence
build — and then the decisions in it genuinely drove the code, which is why it
is kept in the repository rather than discarded.

Nothing here should be read as a record of paid work.
