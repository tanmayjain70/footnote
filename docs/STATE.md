# Where Footnote stands

A handover document. A new session should be able to read this and continue
without re-deriving anything. Update it in the same commit as the work.

_Last updated: 2026-09-13, after the first deployment._

## Status: milestone 1 built, tested, and deployed

| Area | State |
|---|---|
| Database: user-space PostgreSQL 17.11 + pgvector 0.8.6 on port 5433 | Running. Credentials in `%USERPROFILE%\tools\pgsql17\CREDENTIALS.txt`; reproducible with `scripts/dev-db.ps1 install`. **Not a service** — `scripts/dev-db.ps1 start` after a reboot |
| Schema, migration `0001`, two roles, least-privilege startup check | Done; migration round-trips; autogenerate finds nothing |
| Auth, roles, portfolio ACL | Done, tested |
| Providers: hashed + fastembed embeddings, stub + Anthropic answering, pricing | Done, 43 tests, including a fake Anthropic SSE server |
| PDF text extraction, sentence splitter, chunker | Done |
| Ingestion (jobs table, SKIP LOCKED claim, worker thread), documents API | Done |
| Retrieval: document focus, then vector + lexical legs fused by rank | Done. Golden set: right document 100%, right page 94%, MRR 0.69 (was 93% / 33% before the focus stage) |
| Answering (SSE, citation verification), usage and the daily budget | Done |
| Extraction, review, register | Done |
| Evaluation harness (retrieval and end-to-end runs, golden set, from-feedback) | Done |
| Lease generator, golden questions, field registry, three committed samples | Done. 48 leases, 524 pages, 1,728 chunks, 174 + 12 golden questions |
| Demo seed | Done; runs in 3m48s with fastembed; idempotent |
| **Whole API suite** | **328 passed, 96% line coverage**, about six minutes against footnote_test |
| Smoke test against a live seeded server | **28 of 28** (`scripts/smoke-test.ps1`) |
| Web: all seven pages | Done, typechecks and builds; driven in a real browser (Playwright script at `../portfolio-shots/scripts/footnote.js`): sign-in, documents, document detail, a cited answer, a refusal, register, evals, usage, viewer restrictions — clean |
| CI, Docker image, Render blueprint, bootstrap scripts | Exercised: CI runs on every push, the image builds on Render, the blueprint and `deploy.py` both provision the live demo |
| Docs: README, brief, transcript, architecture, deployment, CONTRIBUTING | Done |
| Git | 26 commits, feature-sized, authored by Tanmay Jain alone |
| GitHub | **Private** repository at `tanmayjain70/footnote`, pushed |
| Deployment | **Live.** Web <https://footnote-web-xgu5.onrender.com>, API <https://footnote-api-uuyv.onrender.com>, database on Neon (`footnote`, pgvector 0.8.0). Provisioned by `scripts/deploy.py`; the generated connection strings are in `api/.env.production.local`, gitignored |

## Measured numbers worth quoting

- Retrieval on the golden set (174 answerable): doc hit 1.00, page hit 0.94, MRR 0.69.
- End to end with the extractive stub (186 questions): 90% of answerable
  answered, 7 of 12 unanswerable refused, 7 false answers (3.8%), citation on
  the right document 88%, on the right page 70%, p50 latency ~30 ms, cost $0.
- Extraction: 816 values over 48 leases; 439 confirmed, 3 corrected, 374 pending
  after the seed's review pass.

## Fixed after an adversarial review

A read-only review of the API found twelve real defects near the end of the
build. Each is fixed with a regression test beside it:

- **Cost was looked up by exact model name.** The API answers with the dated
  release behind an alias, so every call would have been priced at zero and the
  daily budget would never have fired. Prices now fall back to the longest
  matching family.
- **`GET /health` returned the budget and today's spend to every role.**
  Director-only now.
- **Extraction spent money without checking the budget** — only answering did.
  Checked when queued and again when the job runs.
- **The login's dummy hash had an invalid salt**, so bcrypt raised instead of
  hashing and an unknown address answered in microseconds against 250 ms for a
  real one: email enumeration by stopwatch. A real digest now, and a disabled
  account hashes too.
- **A question kept serving lease text after its asker lost the portfolio.**
  Answers quote the passages, so a question is now visible only while all of
  its evidence is.
- **A failed end-to-end evaluation deleted the results it had paid for** on
  retry. It resumes instead, and an exhausted budget ends the run rather than
  retrying into the same wall (`errors.Terminal`).
- **Nothing recovered a job whose worker died**; `requeue_stranded_jobs` does.
- **Re-ingesting a document mid-ingest created a second job** racing the first.
- **Re-uploading a document whose ingestion failed did nothing at all.**
- **The register's CSV export could hand a spreadsheet a formula**; cells that
  begin with `= + - @` are written as text.
- **A search for `%` matched every document**; LIKE wildcards are escaped.
- **Evaluation questions flooded the question history** (186 of them); the
  lists now show only what a person asked.

One finding was accepted rather than fixed: the upload size limit is enforced
after the body has been received, because the ASGI server has already spooled
it. The declared length is now rejected first, and the deployment notes say the
real ceiling belongs in front of the application.

## Decisions already made — do not relitigate

- **pgvector in a user-space PostgreSQL.** The system PostgreSQL 17 has no
  pgvector and cannot be modified without admin rights. Rather than storing
  vectors as arrays and searching in Python, the project runs a second
  PostgreSQL 17 built from the official zip binaries with pgvector compiled by
  the local MSVC toolchain. Production (Neon) and CI (`pgvector/pgvector:pg17`)
  have it natively. `scripts/dev-db.ps1` reproduces the setup.
- **Retrieval is two-stage: which lease, then which passage.** A question's
  rare words are matched against document identities (title, metadata, head
  of page 1); two or more hits confine the search to that document (or a tie
  of up to four) and the remaining words rank passages. Measured: page hit
  33% → 94%. Only matches on the document's own name are removed from the
  passage query; first-page matches vote but stay. A focus that does not
  narrow the set is discarded.
- **The lexical leg is OR-ed, ranked by `ts_rank_cd`.** AND semantics returned
  nothing for any question that named a tenant or building.
- **Chunk text is sentences joined with newlines**, so `split_sentences(chunk.text)`
  returns exactly the chunk's sentences and citation block indexes are stable.
  Bare clause numbers are folded into the sentence that follows.
- **Citations come from the API's citation feature over custom-content
  documents**, verified server-side against the sources sent. Zero verified
  citations means `unanswered`.
- **No `fallbacks` / beta headers on the Anthropic request**; the installed SDK
  (1.5) has no typed parameter for them.
- **The stub answerer is a test double, tuned by the harness.** It strips the
  question's proper nouns and document-title words, maps plain English to lease
  vocabulary, and requires half of what remains to appear in a sentence
  (`MATCH_SHARE = 0.5`; 0.6 refused a natural break-notice question). It still
  answers "VAT registration number" with the VAT clause sometimes; the
  harness counts it, and the README says so.
- **Tests use hashed embeddings and the stub answerer.** Retrieval quality is a
  measured number from the harness, not a unit-test assertion.
- **One jobs table, `FOR UPDATE SKIP LOCKED`**, for ingest, extraction and
  end-to-end evaluation runs. No Celery, no Redis.
- **The seed runs behind the server, on a thread of the same process.** A
  second process would hold a second copy of the embedding model, and two do
  not fit in 512 MB.
- **Daily budget enforced before the call**, as a 429; may overshoot by one call.
- **Filename-derived titles are prettified** ("irwell-bank-house" → "Irwell
  Bank House"); anything with its own capitals or digits is left as typed.

## What the first deployment taught us

Three faults that only a 512 MB instance could show, each fixed with a test:

- **Two copies of the embedding model.** The container entrypoint seeded in a
  second process, so two processes each held ~270 MB of model against a 512 MB
  limit. Render killed the pair five times over. The application seeds on a
  thread now: one process, one model, ~270 MB serving and ~400 MB seeding.
- **Nothing swept up after the kills.** `requeue_stranded_jobs` existed and
  nothing called it, so eight leases sat in `running` for ever. The worker now
  sweeps at startup and every five minutes.
- **A half-finished seed looked finished.** The seeder skipped when *any*
  document existed, so the demo would have stayed at eight leases of
  forty-eight. Each stage now skips only its own completed work, and an
  unfinished seed carries on.

## Not verified

- The Anthropic provider against the live API (no key on this machine). Request
  shape and event parsing are tested against a fake server speaking the
  documented SSE format; the document-block schema has not been validated by
  the real endpoint.
- Render free-tier memory with fastembed loaded. Expected to fit in 512 MB with
  one thread; unmeasured.
- The Docker image build, the CI workflow and the Render blueprint have not been
  run (nothing has been pushed).
- Visual layout was checked in screenshots at 1440×1080 only.

## Known limitations worth stating in a conversation

- "Who is the tenant at…" questions miss the parties clause about 3% of the
  time on retrieval: every clause mentions the tenant, and the parties clause
  has no word the others lack. A re-ranker would fix it; the harness will show
  whether it does.
- The extractive stub cannot tell a "registration number" from "registered …
  company number". A model answerer is expected to do better; measure it with
  the same harness before believing it.

## Environment notes

- Python 3.14 venv at `api/.venv`; the embedding model is cached in
  `api/.fastembed_cache` (65 MB, gitignored).
- Test databases `footnote_test` … `footnote_test_6` exist on port 5433 so
  suites can run in parallel; `DATABASE_URL`/`DATABASE_ADMIN_URL` select one.
- The build was orchestrated in short sessions; several were cut off mid-run.
  If a file looks half-finished, check `git status` and this document before
  assuming it is intentional.
