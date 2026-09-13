# Architecture

Three ideas, and everything else is one of them wearing different clothes:
**every claim points at a passage the model was actually given**, **not
knowing is an answer**, and **a person confirms what counts**.

```
  PDF ──upload──▶ document_blobs ──job: ingest──▶ document_pages ──▶ chunks
                  (sha256 per portfolio)                                ├─ embedding  vector(384)  HNSW, cosine
                                                                        └─ tsv        tsvector     GIN

  question ──▶ hybrid search: vector leg ∪ lexical leg, fused by rank, ACL inside the SQL ──▶ 8 passages
           ──▶ the model: one citable document per passage, one block per sentence
           ──▶ every citation checked against the passages that were sent
           ──▶ answered (≥ 1 verified citation)  |  unanswered (none, whatever the prose says)

  document ──job: extract──▶ 17 values, each with a quote and the chunk it came from
           ──▶ evidence checks ──▶ a person confirms, corrects or rejects ──▶ the register

  golden set ──▶ evaluation run (retrieval only, or end to end) ──▶ hit rates, refusals, cost
```

---

## 1. Ingest

### A queue in the database

Uploading stores the bytes and a row in `jobs`. A worker thread in the API
process claims work with one statement:

```sql
UPDATE jobs SET status = 'running', claimed_by = :worker, attempts = attempts + 1, ...
 WHERE id = (SELECT id FROM jobs WHERE status = 'queued'
              ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id
```

`SKIP LOCKED` is the whole mechanism. Two workers — two API instances, or the
worker and the seeder — never claim the same job, and the test that proves it
runs twenty jobs through two threads and counts. There is no Celery and no
Redis: a second process is one more thing to run, to monitor and to explain,
and PostgreSQL already provides a queue that is transactional with the rows
the job produces. Ingest, extraction and end-to-end evaluation runs all go
through the same table.

A job that fails is re-queued until `max_attempts`, then marked failed with
the error on both the job and the document. The derived rows — pages, chunks
— are deleted and rebuilt on every attempt, so a retry after a crash halfway
through cannot leave two copies of page three behind.

### The same file twice is the same document

The upload is keyed on the SHA-256 of the bytes within a portfolio. Uploading a
lease that is already there returns the existing document with `created:
false` and starts nothing. The same bytes in a different portfolio are a
different document, because portfolio membership is what decides who may read
it.

### A scanned lease is refused, not silently indexed

Only the text layer is read. A PDF of pictures has none, and indexing it would
produce a document that answers every question with "not in the documents" for
a reason nobody could see. So it is rejected with a message that says why, and
OCR is a separate piece of work the client chose to defer
([brief.md](brief.md#8-milestones)).

### One sentence splitter

A citation is a block index into a chunk. That only means something if the
sentences the model was shown are the sentences the page viewer highlights
later, so one function — `chunking.split_sentences` — is used at ingest, when a
stored chunk is turned into citable blocks, and when a chunk is displayed. A
chunk's text is its sentences joined with newlines, so splitting a chunk's text
returns exactly the sentences it was built from; a test asserts it. Bare clause
numbers ("3.", "(a)") are folded into the sentence that follows, because nobody
cites a "3.".

Chunks never split a sentence, target about 1,100 characters, and overlap by
one sentence so a fact that straddles a boundary appears whole in one of the
two.

---

## 2. Retrieval

### Which lease first

A question about a lease has two halves. *"When does the term of the Eccles
Insurance Brokers lease at Lancastrian Business Park end?"* names a document
with one half and asks about a clause with the other. Searched as one string,
the cover page and the parties clause outrank the term clause on every measure
— they are where the names are — and the passage that answers is not among
the eight shown to the model. The first version of retrieval did exactly that;
the harness measured it at the right document 93% of the time and the right
page 33%.

So retrieval runs in two stages. The question's rarer words — rarity judged
against the documents the caller can see, not a fixed list, because "Stamford"
is a name in one portfolio and noise in another — are matched against each
document's identity: its title and metadata, and the head of its first page.
When two or more of them single out one document, or a tie of up to four, the
search is confined to those documents and the *remaining* words rank the
passages within them. A question that names nothing runs over everything.
That change took the right page from 33% to 94% on the same golden set. The
misses that are left are mostly "who is the tenant at…", where every clause
mentions the tenant and the parties clause has no word the others lack.

### Two legs, fused by rank

The vector leg orders chunks by cosine distance to the question's embedding
(`pgvector`, HNSW index, `m=16`, `ef_construction=64`). The lexical leg uses
PostgreSQL full-text search over a stored generated `tsvector` with a GIN index,
`websearch_to_tsquery` and `ts_rank_cd`. Each leg contributes its top thirty
candidates.

They are combined by **reciprocal rank fusion** — `score = Σ 1 / (60 + rank)` —
rather than by mixing scores, because a cosine distance and a `ts_rank` are not
on the same scale and any weighting between them would be a number somebody
made up. A chunk both legs found ranks first; a chunk only one found still
surfaces. The top eight go to the model.

Why both legs: leases are full of exact tokens — "Unit 4B", "£42,500", "31
March 2029" — that an embedding blurs and a lexical index nails, and full of
paraphrase — "when does it end" for "shall expire on" — that a lexical index
misses and an embedding catches. Each question is shown which leg found each
passage and at what rank.

### The filter is in the query

Every retrieval query joins `documents` and filters `portfolio_id = ANY(:visible)`,
where the visible set is computed once from the caller's memberships (the
director sees everything). There is no `WHERE` clause for a new endpoint to
forget: the only search function takes the visible set as a required argument
and returns nothing for an empty one.

### Embeddings are computed here

`BAAI/bge-small-en-v1.5` runs in the API process through ONNX Runtime. Nothing
about a document leaves the server to be embedded, which is the answer to the
client's confidentiality question. The tests and CI use a deterministic
feature-hashing embedding of the same width instead: no download, identical
vectors on every run, and retrieval that is lexical rather than semantic —
which is fine, because retrieval *quality* is a measured number from the
evaluation harness, not a unit-test assertion. Do not switch providers on an
existing database without re-ingesting: vectors from two models cannot be
compared.

---

## 3. Answering

### The model is given documents it can cite, not a prompt it can quote

Each retrieved chunk is sent as a document whose content is the list of its
sentences, with citations enabled. The model's answer comes back as text
blocks, each carrying citations that name a document index and a block range.
Nothing is parsed out of prose: the citation is a structured object the API
produced, pointing at a sentence the server sent.

### Every citation is verified anyway

The server checks each one: the document index must be one of the passages it
sent, and the block range must lie within that passage's sentences. Anything
else is dropped, counted (`dropped_citations` on the question) and logged. Then:

| Verified citations | Status |
|---|---|
| one or more | `answered` |
| none | `unanswered` — whatever the prose says |

That second row is the product. An answer with nothing to point at is shown as
"not answerable from the documents you can see", with the closest passages
alongside so a person can judge for themselves. The model's own refusal
sentence, when it gives one, appears inside that card; it is never presented as
an answer.

A model that declines (`stop_reason: refusal`) or an API error marks the
question `failed` with the reason; it is not confused with "the documents do
not say".

### Streaming

The answer streams to the browser as server-sent events over a `fetch` (so the
Authorization header travels with it): `retrieval` first — the passages, so the
right-hand panel fills before the first word — then `text` and `citation`
deltas, then `done` with the stored question. The budget check happens before
the stream opens, so a 429 is an ordinary JSON error rather than a broken
stream.

### The stub answerer

`LLM_PROVIDER=stub` answers extractively: the sentence in each passage that
best overlaps the question, cited, or a refusal with no citation when nothing
overlaps. It produces the same events as the real provider, costs nothing, and
is what the tests and the keyless demo use. The real provider is tested against
a fake server that speaks the documented streaming format — request shape,
citations, refusals, errors — so the parsing is covered without a key.

---

## 4. The register

"Which leases have a break in 2027?" is not a question to ask a chat. It is a
table, and the table has to be trusted.

### Extraction with evidence

For each document the model returns, for each of the seventeen registry
fields, a value **as written**, a verbatim quote, and the id of the chunk the
quote came from (structured output against a fixed schema). The server then
checks the evidence and records the first problem it finds:

| Issue | Meaning |
|---|---|
| `no_evidence` | no value, or a value with no chunk to look in |
| `invalid_chunk` | the chunk named is not in this document — the id is dropped; a foreign chunk is never stored as evidence |
| `quote_not_found` | the quote is not in that chunk (compared without regard to case or spacing) |
| `unparseable` | the value is not a valid date, sum, number, yes/no or registry option; the text is kept for the reviewer |

Values are typed on the way in: `1 April 2024`, `1st April 2024` and
`01/04/2024` all become `2024-04-01`, day-first always, because `04/01/2024`
being read as April is exactly the class of error this product exists to stop.

### A person confirms what counts

Every value starts `pending`. A lease administrator confirms it, corrects it
(the correction is parsed the same way and wins over the model's value), or
rejects it. The register and its CSV export show **confirmed and corrected
values only** by default; an "include unreviewed" switch shows the rest,
marked. The client asked whether unconfirmed values could "just count for now".
No — see [brief.md](brief.md#7-what-the-brief-got-wrong).

The field list is a registry (`extraction_fields.FIELDS`): adding a field is
appending an entry with a label, a type and a description the model can act
on. The stub extractor reads the same registry, using a regular expression per
field that matches the generated leases exactly, so the pipeline from schema to
review to register is tested end to end without a model.

---

## 5. Evaluation

The demo leases are generated, so the truth is known: for every lease, which
page states the rent, the break date, the notice period. From that comes a
golden set — three or four questions per lease with the expected document and
pages, phrased naturally and not always in the lease's own words, plus a dozen
questions a lease never answers.

Two kinds of run:

- **Retrieval only.** For each question: did the expected document appear in
  the top eight, did the expected page, at what rank. Seconds, no model, no
  cost. This is the number to watch when changing chunk size, the fusion
  constant or the embedding model.
- **End to end.** The same, plus the answer: was it answered, did a citation
  land on the right document and page, was an unanswerable question refused,
  how long, how much. This spends money and says so.

Runs are stored with their configuration so two are comparable. A thumbs-down
on an answer can be turned into a golden question in one click, which is how
the set grows from real use rather than from a generator.

---

## 6. Money

Every model call writes a `usage_events` row: tokens in, out, cache read, cache
written, and the cost from a price table per model. Unknown model, cost zero
and a warning.

The daily budget is checked **before** a call, from the sum of today's events
(UTC), and a request over the line gets a 429 that says how much was spent
against what limit and when it resets. The last call of a day can overshoot by
one call; that is documented rather than "fixed" with a reservation scheme that
would be more code than the overshoot is worth.

Spend is in dollars because the provider bills in dollars, and rounding it to
pounds every day would produce a number that agrees with no invoice.

---

## 7. Security and roles

**Two database roles.** `footnote_owner` owns the tables and runs migrations;
`footnote_app` has DML and nothing else, and the API asserts at startup that it
is neither a superuser nor a table owner. CI creates both roles for the same
reason: a single-superuser test database would let the suite pass while the
guarantee quietly did not hold.

**Four application roles**, enforced in dependencies on the server: director,
lease administrator (`admin`), property manager, viewer. Who may upload, ask
(and therefore spend), extract, review, run evaluations and see spend is a
table in [brief.md](brief.md#6-roles); the interface hides buttons, the API
refuses.

**Portfolios.** A document outside the caller's portfolios is a **404, never a
403**: confirming that it exists would leak that it does. The Riverside
portfolio in the demo is being marketed for sale and is visible to two people.

**Tokens.** The refresh token is the only thing persisted in the browser; the
access token lives in memory and dies with the tab.

---

## 8. What was deliberately not built

- **OCR.** About a fifth of the client's archive is scanned. Deferred to its
  own milestone; the tool refuses those files with a clear message instead of
  indexing nothing.
- **Conversation memory.** Each question stands alone. Multi-turn answers reuse
  earlier passages in ways that are hard to cite honestly; deferred until there
  is a corpus of real questions to design from.
- **A re-ranker.** Rank fusion over two legs is enough at this corpus size, and
  the evaluation harness exists precisely so that adding one later is a
  measured decision.
- **A vector database.** pgvector inside the database the firm already runs
  means one backup, one access-control model and one transaction around a
  document and its chunks.
- **Row-level security.** Right for a multi-tenant product; here there is one
  firm, and portfolio membership is one predicate applied in the one place
  retrieval happens.
- **Write-back.** Nothing edits a lease. The register is derived data with a
  review trail, and the source PDF is never touched.
- **Word and Excel ingestion, SSO, expiry reminders.** Additive, and each
  waits on something the client owes (the transcript has the table).
