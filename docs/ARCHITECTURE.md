# Architecture

Five layers, one direction of flow, one authoritative clock, one gate that money passes
through. This document explains *why* each boundary is where it is; the code explains
what happens inside them.

---

## The shape

```
                    ┌──────────────────────────────────────────────┐
                    │  L1  signals          antar/signals/          │
   Razorpay ───────▶│  webhooks · downtime feed · error taxonomy    │
   webhooks         │  Output: AtRiskEvent                          │
                    └───────────────────────┬──────────────────────┘
                                            │
                    ┌───────────────────────▼──────────────────────┐
                    │  L2  detect           antar/detect/           │
                    │  WHY did it fail? Six classes, cost-weighted. │
                    │  Output: Diagnosis (+ recommended class)      │
                    └───────────────────────┬──────────────────────┘
                                            │
                    ┌───────────────────────▼──────────────────────┐
                    │  L3  decide           antar/decide/           │
                    │  WHO benefits, by how much, and is it worth   │
                    │  more than the harm? X-learner CATE → LP.     │
                    │  Output: Decision (may choose nothing)        │
                    └───────────────────────┬──────────────────────┘
                                            │
                              ┌─────────────▼─────────────┐
                              │   PolicyGate               │
                              │   antar/policy/gate.py     │
                              │   Caps · idempotency ·     │
                              │   dry-run · approval       │
                              └─────────────┬─────────────┘
                                            │
                    ┌───────────────────────▼──────────────────────┐
                    │  L4  act              antar/act/              │
                    │  Registered templates. The LLM fills ONE      │
                    │  slot. Sagas unwind partial failures.         │
                    │  Output: ActionRecord (+ DraftedMessage)      │
                    └───────────────────────┬──────────────────────┘
                                            │
                    ┌───────────────────────▼──────────────────────┐
                    │  L5  audit            antar/audit/            │
                    │  Append-only, hash-chained. trace · replay.   │
                    │  Output: the only durable truth               │
                    └──────────────────────────────────────────────┘
```

`antar/pipeline.py` is the only place all five run in sequence. Everything else is a
library.

---

## Why these boundaries

### L1 / L2 — a code is not a cause

Razorpay returns `BAD_REQUEST_ERROR` for a card declined by the issuer, a mandate that
needs authentication, and a customer with no money. Three completely different
situations, one string. L2 exists because *acting on the code* means sending a "please
update your card" message to someone whose card is fine.

The boundary is drawn at "raw provider fact" versus "our interpretation of it", and it
is drawn there so the interpretation can be **wrong and measured**. L2's output is
evaluated on the control arm with a rupee-denominated cost per misclassification, which
would be impossible if diagnosis were fused into ingestion.

### L2 / L3 — "what went wrong" is not "what to do"

L2 says `ISSUER_DOWN` and recommends `WAIT`. L3 decides whether waiting is worth more
than contacting, given this customer, this amount, and this month's contact budget.

Keeping them apart is what lets the ablation in `docs/EVALUATION.md` §12.2 ask whether a
detection component *earns its place in rupees* — and, in M6, delete two of them when
the answer was no. A detector fused into a policy cannot be ablated.

### L3 / PolicyGate — the decision is not the permission

L3 produces a `Decision`. It cannot execute one. Every function that moves money carries
`@requires_gate`, which raises unless it is handed an `ActionRecord` that a gate produced
and did not reject.

This is **N2**, and it is enforced by introspection rather than by a list:
`tests/unit/test_gate_coverage.py` discovers money-moving client methods (those requiring
an idempotency key), discovers executor-shaped functions (those taking an `ActionRecord`),
and fails if any is unmarked. Adding a new executor cannot silently bypass the gate,
because nobody has to remember to add it to anything.

### PolicyGate / L4 — the permission is not the message

The gate approves an *action*: this channel, this amount, this customer, now. L4 turns
that into words. The split means the language model is downstream of every decision that
matters and cannot influence one.

### L4 / L5 — the act is not the record

L5 records. It never decides. The direction is strict: audit reads from the other layers
and none of them read from it. A trace is *derived* from the ledger and never stored, so
it cannot drift from the record and cannot be edited to say something the record does not.

---

## The seams, and what has gone wrong in them

Layer boundaries are where this project's real bugs lived. Every one of these is in
`docs/POSTMORTEM.md` with the full account.

| Seam | What went wrong |
|---|---|
| Simulator → L2 | The detector read `batch.true_failure_class`. Recall 1.000, because it had the answer key (**D10**). |
| L2 → L3 | For a while L3 received `diagnosis=None`, so detection had no influence and its retention CI was exactly (0, 0) — a verdict that could not have been anything else (**D16**). |
| L3 → L4 | L3 could select `VOICE`; L4 had no template for it, then had a template that promised a retry AFA cannot deliver (**D20**). |
| Gate → L5 | `ledger or NullLedger()` discarded the caller's *empty* ledger, so every gate decision went to a sink (**D21**). |
| Evaluation → pipeline | The production path valued candidates from the simulator's ground truth while recording the model's estimate (**D22**). |

The pattern is consistent enough to state as a rule: **a unit test looks inside a layer;
only an integration test looks at a seam.** `tests/integration/test_full_batch.py` exists
because four of the five above would have been caught by it and were not caught by
anything else.

---

## Cross-cutting: one clock

`antar/clock.py` is the only source of the current time in the codebase. `datetime.now()`
appears nowhere outside it — `docs/CLOCK_AUDIT.md` records the sweep that established
that, and a test keeps it true.

The reason is regulatory. `TRAI-01` (the 10:00–21:00 window) and `RBI-EM-01` (pre-debit
lead time) are both predicates on *when*. A worker with a fast system clock would decide
that 09:58 is 10:08 and send inside the quiet band. With one authoritative clock, a skewed
worker is a skewed installation rather than a skewed decision path, and
`tests/chaos/test_chaos.py` asserts exactly that.

IST is a fixed `+05:30` offset rather than a `zoneinfo` lookup (**ADR-0004**): India has
no DST, and depending on the host's tzdata for a compliance predicate is a dependency
nobody would choose deliberately.

---

## Cross-cutting: regulations as data

`antar/policy/regulations.py` holds eighteen rules, each a pure predicate on
`DecisionContext` with a primary-source citation and a verification level. Eleven are
`BLOCKING`, and the constructor **refuses** to build a `BLOCKING` rule whose verification
rests on a secondary source:

```python
if self.severity is Severity.BLOCKING and self.verification not in VERIFIABLE_ENOUGH_TO_BLOCK:
    raise ValueError(f"{self.id} is BLOCKING but only {self.verification.value}-verified. ...")
```

Verifying against primary sources before encoding corrected six rules. The most expensive
correction: the contact window opens at **10:00**, not 09:00, because TCCCPR Schedule II
para 3(1) Note-1 makes the 08:00–10:00 band default-off for every customer. That hour is
priced in the shadow-price panel, which is the closest this project comes to a joke.

Because the rules are pure predicates, `antar/policy/compiler.py` can turn them into LP
rows, `tests/property/` can generate contexts at random, and
`docs/REGULATORY_REGISTER.md` can be *generated* from the code rather than maintained
alongside it.

---

## Cross-cutting: the LLM's blast radius

**N1: the LLM writes language only.**

Mechanically: `act/templates/registry.yaml` owns every message body. `DraftContext.fixed_slots()`
supplies every amount, date, URL and name from the `Decision`. The model is asked for one
slot — `reason` — and `_parse()` discards any other key before rendering rather than after.

The consequence is that a successful prompt injection is a non-event. Persuade the model
to emit `{"amount": "10000"}` and nothing happens: `amount` is not a slot it may fill, and
the rendered message carries the amount the allocator decided.

The remaining risk is *tone*, and that is what `act/contamination.py` is for — TRAI-03
says promotional content contaminates a whole message, and "we also have 20% off annual
plans" is a genuinely helpful sentence that is also a regulatory violation. The detector
is rules-first: a rule match pins the score at 1.0 and no classifier confidence can lower
it (**ADR-0022**).

---

## Data flow for one event

```
webhook ──▶ AtRiskEvent ──▶ Diagnosis ──▶ Candidate ──▶ CandidateValue
                                              │              │
                                              └──▶ LP ◀──────┘
                                                    │
                                              Decision (or abstain)
                                                    │
                                          ┌─────────┴─────────┐
                                     is_control?          chosen action
                                          │                   │
                                     record only        DraftContext
                                          │                   │
                                          │              PolicyGate
                                          │                   │
                                          │              ActionRecord
                                          └─────────┬─────────┘
                                                    ▼
                                           LedgerEntry × 4-5
```

Every arrow is a frozen Pydantic model in `antar/signals/schemas.py`. Money is integer
paise everywhere; there is no float rupee anywhere in the decision path.

---

## Storage

SQLite by default (**ADR-0006**), because `make evaluate` has to run from a clean checkout
with no Docker. The ledger uses append-only triggers as a speed bump and `verify_chain()`
as the actual control — anyone who can drop a trigger can drop those two, which is why the
tamper tests do their damage with the triggers disabled.

Postgres is available through `docker-compose` for the API path and is **not** wired to
the ledger.

---

## What is deliberately not here

- **No queue.** A batch runner is enough for the claim being made, and a queue would add
  operational surface without adding evidence.
- **No feature store.** Features are built from the event, the customer context, and the
  diagnosis, all of which arrive in the request. A store would add a leakage surface —
  `tests/statistical/test_no_leakage.py` exists because that surface is where D10 lived.
- **No online learning.** The model is fitted per run and versioned into every decision it
  makes. A model that updates itself between two decisions makes those two decisions
  incomparable, and the whole evaluation rests on comparing them.
- **No retry orchestrator.** Razorpay already retries. Antar decides *whether the retry
  should be accompanied by a contact*, which is a different question and the only one it
  claims to answer.
