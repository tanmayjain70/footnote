# The scoping exchange, in full

> **Simulated.** There is no real client. "Hallam & Pryce" is not a real
> company. This is a written exercise in scoping a retrieval build over legal
> documents: a deliberately realistic job post, the questions that should be
> asked of it, and the negotiation that follows. Both sides were written as an
> exercise. It is kept because the decisions in it drive the code.
>
> The distilled version — decisions only, no dialogue — is
> **[brief.md](brief.md)**.

Four parts: the job post, the reply that questioned it, the answers, and the
revised scope.

---

# Part 1 — The job post

**Title:** Ask questions of 1,400 leases. It must cite the page, and it must say "I don't know".

**Budget:** $9,500 fixed-price · **Timeline:** 8 weeks · **Experience level:** Expert

> We manage commercial property. About 380 units across the North West, for
> around 40 landlords, run from an office in Manchester. Every unit has a lease
> and most have more than that — deeds of variation, side letters, renewal
> notices, licences to alter. About 1,400 documents, all PDFs, in a folder
> structure that made sense to whoever set it up.
>
> A contractor set us up with one of those chat-with-your-PDFs tools. In March
> a landlord wanted an industrial unit at Trafford Park back at the five-year
> break. It told a lease administrator the break notice was three months. It
> was six. We served notice late and the tenant is now locked in for another
> five years. The clause was on page 7 of a PDF the tool had been given.
>
> **I do not need it to be clever. I need it to point at the page, and I need
> it to say 'I don't know' when it doesn't.**

## What it needs to do

### 1. Answer questions about a lease

"What is the rent review basis for Unit 4, Meridian House?" "When does the
Whitworth Court lease expire?" "Who is the guarantor at 12 Deansgate?" With the
page it came from, every time.

### 2. Answer questions across leases

"Which leases have a break in 2027?" "Which Northern Estates tenants have a
guarantor?" This is what the property managers ask most and it is what the old
tool was worst at — it would answer from three leases and leave out the other
nine.

### 3. Say when it does not know

The old tool never once said it did not know.

### 4. Take the documents as they are

A folder of PDFs. We are not retyping anything and we are not tagging 1,400
files by hand.

## Users

Eight: me, two lease administrators, four property managers, and our finance
lead, who needs to read the rent figures and nothing else.

## Stack

We already run PostgreSQL for the rent ledger. React and Python are what our
last contractor used. Hosted somewhere that is not my problem.

---

# Part 2 — The reply that questioned it

> The sentence that matters is *"point at the page, and say 'I don't know' when
> it doesn't"*. Most tools in this category do neither — not because it is
> hard, but because a tool that refuses looks worse in a demo than one that
> guesses. You have paid for the demo version once already.
>
> Fourteen questions. The ones that change the price are marked.

## About what an answer is

**1. What does "point at the page" mean to you?** Two things a tool can do
here look identical on screen until the day one of them is wrong. (a) The
model writes "(page 7)" into its answer — a claim, made by the same thing that
just invented a notice period. (b) The model may cite only passages it was
shown, and every citation is checked against that passage before you see it;
one that does not match is dropped and counted. The tool that cost you the
break did (a). I would build (b) or not build it. This does not change the
price; it is the price.

**2. What should happen when the answer is not in the documents?** Three
behaviours. Answer anyway with a warning — what you had. Say "I don't know"
and stop. Say "I don't know" and show the passages it looked at, so a person
can see in ten seconds whether the answer is nearly there or nowhere. I would
build the third: an answer with no verified citation is presented as *not
answerable*, never as an answer.

**3. Which questions are actually being asked?** *(changes the price)* "What is
the break notice at Whitworth Court 2B" is a chat question: one lease, one
clause, one page. "Which leases have a break in 2027" is not — a chat reads a
few passages from a few leases and confidently omits the rest, which is
exactly what you describe. It is a table, and a table is built differently:

| | Meaning | Cost |
|---|---|---|
| (a) | Single-lease lookups with verified citations | The core |
| (b) | Plus a register: the key terms extracted from every lease, each with a quote and a page, **confirmed by a person**, filterable and exportable | ~2 weeks |
| (c) | Plus cross-document synthesis in the chat — "summarise the rent review positions across Northern Estates" | 3–4 weeks, costly to run, hard to verify |

**4. Who confirms a value before it counts?** If I extract "break notice: 6
months" from 380 leases, some will be wrong — a deed of variation changed it,
or the model read the landlord's break instead of the tenant's. Somebody has to
look at each value next to its quote and say yes. Twenty terms across 380
leases is six or seven thousand values; at half a minute each, a week and a
half of one person's time. Whose?

## About the documents

**5. What are the 1,400 documents, physically?** *(changes the price)* A PDF
produced from Word has text in it. A PDF from a scanner is a photograph of
text, unreadable without OCR — a separate step with its own errors, and a "6"
read as "8" in a notice period is the failure you have already had, in a new
place. What proportion of the archive is scanned? The older leases usually are.

**6. Who may see what?** Forty landlords. Is every property manager entitled to
read every landlord's leases? If any portfolio is confidential — being sold, in
dispute, a landlord who has asked — that changes the data model rather than
adding a checkbox: a question over "all the leases" must exclude the ones you
cannot see, silently, in the search itself.

**7. Who may upload, and who may delete?** A wrong document in the index is a
wrong answer with a real-looking citation. Upload an unsigned draft next to the
signed lease and the tool will cite the draft in good faith. I would restrict
upload to the lease administrators and refuse a file that is already there.

## About money and where the text goes

**8. How many questions a day, and who may ask?** *(changes the running cost,
not the build)* Every question sends passages to a hosted model that charges
per word in and out; building the register costs money once per lease.
Neither is large, but a tool with no spending cap is one somebody will leave a
script running against, and you will find out from the invoice.

**9. Is it acceptable for passages of your leases to go to a third-party model
over an API?** *(changes the price)* The alternative is a model on your own
server, and that is a different project: a GPU machine to run and pay for, two
or three weeks more, and the models that fit on one are worse at precisely the
thing you care about — not making things up. Decide this on purpose, with the
provider's terms in front of you.

## About knowing it works

**10. How will you know it is right?** The old tool was wrong once, visibly,
and presumably many times before that, invisibly. I would want a set of
questions with known answers — which document, which page — and a set the
leases genuinely cannot answer, with every change to how passages are found
scored against both. Somebody who knows the leases has to write the first set.

**11. What happens when a user thinks an answer is wrong?** A thumbs-down with
a reason is cheap to build, and it is the only way the question set in 10 grows
with the questions people actually ask.

## About scope and risk

**12. Is anything acting on the answers?** Nothing in the brief sends a notice,
sets a reminder, or writes to another system. I want that confirmed, because
"and then it emails the manager three months before the break" is the obvious
next request, and a reminder driven by an unconfirmed date is the incident you
have already had, automated.

**13. Who owns this in a year?** The model API key, the server, the user
accounts. You will want sign-in through your Microsoft 365 eventually; is that
now, and who at your end would set it up?

**14. On the budget.** *(changes the price)* Everything above is $24,000–28,000
at my rate. Section 4 proposes what $9,500 buys.

---

# Part 3 — The client's answers

**1.** **(b).** *"The old one did the first thing. I did not know there was a
second thing."*

**2.** **Show the passages.** *"If it doesn't know, I want to see what it
looked at, so Owen can decide in ten seconds instead of going back to the
folder."*

**3.** **(b).** The register is what the property managers actually want;
"which leases expire in Q2 2027" comes in from landlords every quarter and is
currently answered from a spreadsheet nobody trusts. *"Nobody asked for (c).
The last tool tried to do (c) and that is how we got here."*

**4.** **Owen Pryce-Reid**, the senior lease administrator. A week and a half
is accepted — *"it is less than the break cost us"*. One condition: he wants to
see unconfirmed values while he works through them, but marked, so nobody
quotes one to a landlord.

**5.** **About a fifth are scanned** — the pre-2015 leases, mostly. The client
had not known this was a distinction. Accepted that those are refused with a
clear message rather than silently indexed as empty, and dealt with separately.

**6.** **Yes.** The **Riverside** portfolio — ten units on the Irwell — is
being marketed for sale. Only Priya Hallam, the managing director, and **Tom
Whitlock**, the manager who runs it, may see it; the other managers and finance
must not be able to tell it is in the system at all. Everyone else sees the
portfolios they are assigned: **City Centre** and **Northern Estates**.

**7.** Upload and delete: **lease administrators and the director only**.
Managers ask questions. Finance reads.

**8.** Thirty or forty questions a day between the four managers, more in the
first month. Finance does not need to ask. And then: *"What stops this costing
us £2,000 in a month?"*

**9.** *"Our leases are confidential to our landlords. Are they being used to
train someone's model? What actually leaves our server?"* Answered in Part 4.

**10.** *"How will I know it is not getting worse? The last one got worse and
nobody could tell."* Owen will write **forty questions with the page the answer
is on, and ten the leases do not answer**.

**11.** Yes.

**12.** Confirmed: **nothing acts on an answer**. *"Reminders later, once the
dates are confirmed. I have learned that one."*

**13.** IT is an outsourced provider; Microsoft 365 sign-in *"when they can be
bothered"*. Priya holds the API key. Hosting is, still, not her problem.

**14.** *"What does $9,500 get me, and what would you cut?"*

---

# Part 4 — The revised scope

## What the budget buys

**$9,500, eight weeks**, being the three things the client actually asked for:

1. **Cited answers over the text PDFs** — the model may cite only the passages
   found for the question, every citation is verified against the passage it
   names before it is shown, and an answer with no verified citation is
   presented as *not answerable* with the closest passages beside it.
2. **The register** — seventeen key terms extracted from every lease, each with
   a quote and a page, confirmed, corrected or rejected by a lease
   administrator before it counts; filterable by expiry and break; exported to
   CSV. Unconfirmed values are visible on request, marked, and excluded by
   default.
3. **Portfolio confidentiality** that lives in the search query rather than
   the screen: a document outside your portfolios does not exist, in search or
   by address.

Plus the daily spending cap, the metering behind it, and the evaluation
harness, because they answer the three questions the client asked back and are
cheap once the rest exists. A PDF with no extractable text is refused with a
message that says why. A file already in a portfolio is refused as a duplicate.

That is milestone 1 and it is what this repository contains.

## What was moved out, and why

| Deferred | Reason | Estimate |
|---|---|---|
| OCR for the scanned fifth of the archive (~280 documents) | Waits on a sample of the scanned leases and a decision on how OCR errors are reviewed; the text pipeline is unchanged by it | $3,500 |
| Break-date and expiry reminders by email | Waits on the register being confirmed — a reminder from an unconfirmed date is the incident again — and on a mailbox to send from | $1,200 |
| Word and Excel ingestion (side letters, rent schedules) | Waits on samples; the ingest pipeline takes a new parser without rework | $1,500 |
| Sign-in through Microsoft 365 | Waits on the IT provider registering the application; local accounts are built to be swapped out | $2,500 |
| Multi-turn conversation memory | Additive; every question is stored with what the model saw, so a follow-up can be built on top | $2,000 |
| Cross-document synthesis in chat — answer (c) | The register answers the question that was actually asked; (c) can be added over it and would need its own verification | $5,000+ |

## What was added, and the question that prompted each

**"What stops this costing us £2,000 in a month?"** A daily budget, set by the
director and enforced on the server: every model call is metered — tokens in,
tokens out, cost, who, what kind — and once the day's budget is spent, asking
stops with a clear message until midnight UTC. The provider bills in dollars,
so the budget is in dollars; everything in the leases is in pounds, and the
two are not confused. For scale: a question shows the model about eight
paragraphs and costs a penny or two; extracting the register once across the
whole archive is on the order of a hundred dollars. The cap exists for the day
someone leaves a script running.

**"Are they being used to train someone's model? What actually leaves our
server?"** For a question: the handful of passages found for it — typically
eight paragraphs, not the document. For the register: the text of one lease at
a time, once. The index used to find passages is computed on your own server;
nothing is sent anywhere to build it. The model is used through its API under
terms that do not train on what is sent. Where the provider's servers are and
how long it retains requests are in its terms, not in my gift, and should be
read before sign-off.

**"How will I know it is not getting worse?"** A golden question set — Owen's
forty with their pages, and his ten that have no answer — and a harness that
scores any run against it: was the right document found, the right page, at
what rank; did the answer cite the right page; were the ten refused; how often
did it answer when it should not have. A thumbs-down on a real question can be
promoted into the set. A change to how passages are found is measured against
it before it is deployed, not felt afterwards.

## The one thing that was refused

Late in the exchange:

> "Can the unconfirmed values just count for now? Owen has 380 leases to get
> through and the managers want the table this month."

No. The old tool's mistake was a single unverified value presented as fact. A
register of six and a half thousand unverified values presented as fact is the
same mistake with a spreadsheet attached, and it is the one report a landlord
will be shown.

What shipped instead makes the review fast rather than optional: a queue
ordered by document, the quote and the page beside every value so confirming
one is a glance, a pending count in the navigation that does not go away, and
unconfirmed values visible when asked for — marked, and left out of the
register and its export by default.
