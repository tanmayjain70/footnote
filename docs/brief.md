# The client brief, and what changed after asking

> **This is a simulated engagement.** Footnote is a personal project. There is
> no real client, no money changed hands, and "Hallam & Pryce" is not a real
> company. The brief was written as a realistic exercise in scoping a
> retrieval build over legal documents, and the decisions in it genuinely
> drive the code — so it is kept here rather than discarded.

The full exchange it was distilled from is in
**[brief-transcript.md](brief-transcript.md)**.

Its purpose is practical. Why a citation is verified against the passage the
model was shown rather than read out of its prose, why an answer with no
verified citation is a refusal, why "which leases expire in Q2 2027" is a
table and not a chat, why a document you cannot see is a 404, and why
unconfirmed values were refused a place in the register — none of that is
derivable from the code.

---

## 1. The premise

A commercial property manager in Manchester: about 380 units across the North
West for around 40 landlords, and roughly 1,400 documents — leases, deeds of
variation, side letters, renewal notices. Two lease administrators, four
property managers, a finance lead, a managing director.

> "A contractor set us up with one of those chat-with-your-PDFs tools. It told
> a lease administrator the break notice was three months. It was six. We
> served notice late and the tenant is now locked in for another five years."

The clause was on page 7 of a PDF the tool had been given. That is the whole
product in one paragraph. The client is not buying a chatbot; they are buying
**an answer they can check, and an honest refusal when there is not one**.

Stated plainly by the client, and worth repeating because it is the acceptance
criterion for everything here:

> **"I do not need it to be clever. I need it to point at the page, and I need
> it to say 'I don't know' when it doesn't."**

---

## 2. What an answer is

A citation the model writes into its own prose — "(page 7)" — is a claim made
by the same thing that just invented a notice period. So here a citation is
something else:

- The passages found for a question are recorded, with their position in the
  list the model was shown.
- The model may cite only those passages, by position and by sentence range.
- Every citation is checked against the passage it names before it is shown.
  One that does not match is dropped, and the drop is counted.
- **An answer with zero verified citations is presented as "not answerable"**,
  with the closest passages beside it, so a person can see in ten seconds
  whether the answer is nearly there or nowhere.

The client chose the last point over a bare "I don't know":

> "If it doesn't know, I want to see what it looked at, so Owen can decide in
> ten seconds instead of going back to the folder."

---

## 3. What is a chat question, and what is a table

"What is the break notice at Whitworth Court 2B?" is one lease, one clause,
one page. "Which leases have a break in 2027?" is not — a chat answers it from
a few passages of a few leases and confidently omits the rest, which is how
the old tool behaved.

So cross-document questions are answered by a **register**, not the chat:
seventeen terms per lease — tenant, landlord, address, unit, term start and
end, term length, annual rent, rent review basis and date, break date and
notice period, repairing obligation, permitted use, deposit, guarantor, VAT
election — each extracted with a verbatim quote and a page number, and **each
confirmed, corrected or rejected by a lease administrator before it counts**.

Unconfirmed values are visible, on request and marked, so the administrator
can work from the queue; they are excluded from the register and its export by
default. The register filters by expiry date and by the presence of a break,
which are the two questions landlords ask every quarter.

---

## 4. Confidentiality, which was nearly missed

The brief said eight users and nothing about who could see what. The answer to
question 6 was that the **Riverside** portfolio is being marketed for sale and
may be seen by the managing director and one property manager only; the other
managers and finance must not be able to tell it is in the system.

That is not a checkbox. It changes the data model:

- Documents belong to portfolios; users are members of portfolios; a director
  sees all of them.
- **The portfolio filter lives inside the retrieval query**, not in the
  request handler, so a new endpoint cannot forget it. A search over "all the
  leases" silently means "all the leases you can see".
- A document outside your portfolios is a **404, never a 403** — a 403
  confirms the document exists, which for a portfolio being sold is the leak.
- The same visible set governs the document list, the register and the review
  queue.

---

## 5. Decisions that drive the schema

**What the model was shown is stored with every question.** The retrieved
passages, their ranks from each search leg, and every verified citation with
its sentence range are recorded against the question. An answer can be
audited a year later without re-running it, and the evaluation harness reads
the same rows.

**Sentences are split by one deterministic function, at ingest and at answer
time.** A citation names a sentence range within a passage; if the split
differed between the two moments the range would point at the wrong text.

**Reviewed values are never overwritten.** A correction is stored beside the
extracted value, with who made it, when, and a note. The extracted value, its
quote and its page stay, so a corrected value still shows what the model read
and how the person disagreed.

**Every model call is metered, and the budget is checked before anything else
happens.** Tokens in and out, cost, kind of call, who made it. Once the day's
budget is spent, a question is refused before any passage is retrieved. The
provider bills in dollars, so the budget is in dollars; the leases are in
pounds, and the two are never mixed.

**The original bytes are kept.** Re-ingesting after a change to chunking or
embedding needs no re-upload, and the content hash keeps the same file from
entering a portfolio twice.

**A PDF with no extractable text is refused, not indexed as empty.** About a
fifth of the archive is scanned. An empty document in the index is one the
tool will confidently say nothing about, which is worse than a clear message
saying OCR is not part of this milestone.

**The index is computed locally.** Embeddings are produced on the server;
nothing is sent to the model provider to build the index. Only the passages
retrieved for a question — or the text of one lease at a time during
extraction — leave the server, under API terms that do not train on the data.

---

## 6. Roles

Eight people, four roles.

| Role | Who | May do |
|---|---|---|
| **Director** | Priya Hallam, managing director | Everything: every portfolio, evaluations, usage and the daily budget, other users' questions |
| **Admin** | Lease administrators — Owen Pryce-Reid | Upload, delete and re-ingest documents; run extraction; review values; ask questions |
| **Manager** | Property managers — Sadia Mahmood, Tom Whitlock | Ask questions and give feedback on answers in their portfolios; read the register |
| **Viewer** | Grace Adeyemi, finance | Read the register, documents and pages. **Cannot ask** — asking spends money |

Portfolio membership is separate from role: Tom Whitlock sees Riverside and
nothing else; Sadia Mahmood sees City Centre and Northern Estates; finance
sees the same two. Only the director sees everything without being a member.

---

## 7. What the brief got wrong

**"Point at the page" was the whole requirement, and the brief thought it was
a feature every chat tool has.** The tool that cost them the break also
printed page numbers. The difference between a page number the model asserts
and a citation checked against the passage it was shown is the product.

**Cross-document questions were listed as a chat feature.** They are a table
with a person in the loop (section 3). The client's own words: *"The last tool
tried to do (c) and that is how we got here."*

**A fifth of the archive is scanned.** Not mentioned; the client had not known
it was a distinction. It moves OCR to its own milestone and puts a clear
refusal in front of the user instead of an empty index.

**Confidentiality was not mentioned.** It changes the data model fundamentally
(section 4).

**Nobody had thought about what a question costs**, until the client asked
*"what stops this costing us £2,000 in a month?"* — and nobody had thought
about where the text goes, until *"are they being used to train someone's
model?"*. Both answers are in the code, not in a policy document.

**The budget.** $9,500 against a $24,000–28,000 scope, resolved by cutting
scope rather than rigour — see the deferral table in
[brief-transcript.md](brief-transcript.md#part-4--the-revised-scope).

**One request refused.** *"Can the unconfirmed values just count for now?"*
No: the old tool's mistake was one unverified value presented as fact, and a
register of six and a half thousand of them is the same mistake with a
spreadsheet attached. What shipped instead makes review fast rather than
optional — the quote and the page beside every value, a pending count that
does not go away, and unconfirmed values visible only when asked for and
always marked.

---

## 8. Milestones

| # | Scope | Status |
|---|---|---|
| **1** | Text-PDF ingest, hybrid retrieval with the portfolio filter, cited answers with verified citations and an honest refusal, the seventeen-term register with human review and CSV export, daily budget and metering, evaluation harness with a golden set, feedback on answers | **This repository** |
| 2 | OCR for the scanned fifth of the archive | Deferred — waits on a sample of the scanned leases |
| 3 | Word and Excel ingestion; break-date and expiry reminders by email | Deferred — waits on samples, and on the register being confirmed |
| 4 | Sign-in through Microsoft 365 | Deferred — waits on the IT provider; local accounts built to be swapped out |
| 5 | Multi-turn conversation memory; cross-document synthesis in chat | Deferred — additive, no rework |
