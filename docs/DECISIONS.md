# DECISIONS.md — Architecture Decision Record log

Append-only. Never edit a prior entry; supersede it with a new one and mark the old
one `Superseded by ADR-nnnn`.

Format: **ID · date · title · status** — context, decision, consequence.

---

## ADR-0001 · 2026-08-22 · Why Track 03 · Accepted

**Context.** Four tracks were open. Track 03 (AI Revenue Recovery) asks for "measured
money recovered across a batch, with compliant escalation, stopping rules, and an
audit trail."

**Decision.** Build Track 03, and treat the word *measured* as the load-bearing one.
The obvious build for this track is a dunning agent with a retry schedule. That build
cannot answer the only question that matters — how much of the recovered money would
have arrived anyway — because it has no control group. We build the measurement
apparatus first and the recovery policy second.

**Consequence.** A randomised holdout exists from the first batch, which costs us 20%
of the treatable population and buys the only defensible number in the submission.
Gross recovery is reported alongside incremental recovery everywhere, so the gap is
always visible.

---

## ADR-0002 · 2026-08-22 · Python 3.13 on the build machine, 3.11 as the floor · Accepted

**Context.** PLAN.md section 5 specifies Python 3.11 for the econml/scikit ecosystem.
The build machine has 3.13.2 and no 3.11 interpreter.

**Decision.** Target `requires-python = ">=3.11"`, develop on 3.13, pin CI and the
Docker image to 3.11. econml 0.16, LightGBM 4.6, and PuLP 3.3 all import and run on
3.13; where a library behaves differently, CI on 3.11 is the arbiter.

**Consequence.** `StrEnum` (3.11+) is used freely. Any 3.12+ syntax is banned so the
3.11 floor stays real. PLAN.md section 5 updated in the same commit, per rule 5.

---

## ADR-0003 · 2026-08-22 · `tasks.py` is the build system; the Makefile is an alias · Accepted

**Context.** N4 requires `make evaluate` to reproduce every number from a clean
checkout. The build machine is Windows and has no `make`.

**Decision.** Every target is implemented once, in `tasks.py`, invoked as
`python tasks.py <target>`. The `Makefile` contains no logic; each rule shells out to
`tasks.py`.

**Consequence.** The reproducibility claim does not depend on a build tool being
installed. `make evaluate` and `python tasks.py evaluate` are the same code path, so
they cannot drift apart.

---

## ADR-0004 · 2026-08-22 · IST as a fixed +05:30 offset, not a zoneinfo lookup · Accepted

**Context.** `TRAI-01` confines contact to 09:00–21:00 recipient-local time, and
`RBI-EM-01` imposes a 24-hour lead time. Both need a timezone. `zoneinfo` requires
the `tzdata` package on Windows, adding a dependency to a legally load-bearing path.

**Decision.** Model IST as `timezone(timedelta(hours=5, minutes=30))` in
`antar/clock.py`. India observes no daylight saving and has a single timezone, so a
fixed offset is not an approximation — it is exact.

**Consequence.** No `tzdata` dependency. If Antar is ever extended beyond India this
decision must be revisited, and `CustomerContext.timezone_offset_minutes` is the seam
where that would happen.

---

## ADR-0005 · 2026-08-22 · Deterministic content-derived ids instead of random ULIDs · Accepted

**Context.** PLAN.md section 9.3 requires a fixed seed to produce a byte-identical
event stream and an identical ledger hash chain. Random ULIDs break that immediately.

**Decision.** `antar/ids.py` derives every id as a blake2b digest of its content,
rendered in Crockford base32. Same inputs, same id, on any machine.

**Consequence.** Ids are stable across reruns, which makes the ledger chain hash a
genuine regression signal and makes idempotency keys reconstructible by a restarted
worker rather than needing to be persisted first. The cost is that two genuinely
distinct events with identical content collapse to one id; the id inputs therefore
always include the cycle number and timestamp.

---

## ADR-0006 · 2026-08-22 · SQLite by default, Postgres in Docker · Accepted

**Context.** N4 requires `make evaluate` to run from a clean checkout. Requiring
Docker and a running Postgres to reproduce a number adds a failure mode between the
reviewer and the evidence.

**Decision.** `storage.database_url` defaults to a local SQLite file.
`docker-compose.yml` overrides it with Postgres. The append-only ledger constraint is
enforced by a trigger in both dialects, written once per dialect in
`antar/audit/ledger.py`.

**Consequence.** A reviewer with only Python can reproduce every number. The
production story still uses Postgres, and the ledger's append-only guarantee is
tested against whichever engine is configured.

---

## ADR-0007 · 2026-08-22 · Synthesised fixtures, with a one-command path to real ones · Accepted

**Context.** PLAN.md M1 requires recorded payloads from a live Razorpay test-mode
account, and predicts several test-mode/live divergences to be found in week one. No
test-mode credentials were available for this build.

**Decision.** Synthesise fixtures from the public documentation
(`scripts/make_fixtures.py`), label their provenance loudly in
`tests/fixtures/README.md` and `docs/LIMITATIONS.md` L1, and write
`scripts/record_fixtures.py` so that a single command with real keys replaces them
and prints every structural divergence. `tests/conftest.py` prefers `recorded/` over
`webhooks/`, so the swap needs no test changes.

**Consequence.** The integration layer is structurally tested and not empirically
tested, and the repo says so rather than implying otherwise. The M1 acceptance
criterion "document every test-mode divergence" is recorded as **not measured**, not
as zero. This is the single largest known gap in the build.

---

## ADR-0008 · 2026-08-22 · Detection cost is denominated in actions, not labels · Accepted

**Context.** PLAN.md M3 asks for per-class precision and recall reported with "the
false-positive cost in rupees". The first implementation indexed that cost by predicted
label, which produced a self-contradictory ablation: adding evidence appeared to raise
total cost, because converting an `UNKNOWN` into a wrong `ISSUER_DOWN` was charged more
than leaving it unresolved — although both recommend `WAIT` and the merchant does the
same thing either way.

**Decision.** Cost is `ACTION_COST[(InterventionClass, FailureClass)]` — the price of
the action the diagnosis recommends, given the true cause. Every cell carries a written
rationale and a test asserts none is unpriced.

**Consequence.** A detector is graded on what its output causes to happen, not on the
name it assigns. The `UNKNOWN` class stops being artificially cheap, which is the point:
`UNKNOWN` routes to `WAIT`, and `WAIT` is genuinely expensive when the instrument is
broken. See POSTMORTEM D11.

---

## ADR-0009 · 2026-08-22 · 30% of simulated outages are never declared · Accepted

**Context.** The detection layer cross-checks failures against the Downtime API. In the
simulator, the same registry decided which debits failed. Handing the detector the
generator's own bookkeeping produced an `ISSUER_DOWN` recall near 1.00 that measured
nothing.

**Decision.** `simulator.downtime.declared_share` (0.70) splits injected outages into a
declared registry, which is all `antar/detect` may see, and a full one used to decide
failures and to grade the detector afterwards. `SimulatedBatch.downtime` is the former;
`SimulatedBatch.all_downtime` is the latter and is evaluation-only.

**Consequence.** The undeclared 30% is what the changepoint detector exists to catch,
and how much of it that detector actually catches is now a measurement rather than an
assumption — currently 26 of 212 windows, which `docs/LIMITATIONS.md` L9 reports
honestly. It is also simply more realistic: a downtime feed lags reality and misses
smaller regional incidents.

---

## ADR-0010 · 2026-08-22 · Regulations verified against primary sources at encoding time · Accepted

**Context.** PLAN.md section 3 deferred verification to "before the final commit". Deferring
it means the constraint compiler, the allocator, and every downstream number are built on
thresholds nobody has checked — and a threshold that moves changes the constraint rows.

**Decision.** Verify before encoding. Every rule in `regulations.py` carries a
`Verification` status, and `Regulation.__post_init__` **raises** if a `BLOCKING` rule rests
on anything weaker than a primary document or a full-text gazette reproduction. The
constraint is structural, not a test that could be deleted.

**Consequence.** Verification found six errors in our own specification, one of them
material: the TCCCPR contact window opens at **10:00**, not 09:00, because the 08:00–10:00
band is default-OFF for every customer. Antar has an hour a day less contact capacity than
planned, which *raises* the shadow price on a contact slot. Three rules dropped to
`ADVISORY` for want of a primary source. `TRAI-06` as written in the plan would have
encoded wrong law — an unconditional DND block on transactional messaging.

The corrections are annotated inline in PLAN.md rather than silently edited out, because
the plan having been wrong is the argument for verifying early.

---

## ADR-0011 · 2026-08-22 · Infeasible candidates are removed, not constrained · Accepted

**Context.** A regulation could be expressed either as an LP constraint row or as a filter
that removes the candidate before the solver runs.

**Decision.** Per-candidate regulatory predicates remove the candidate. Only genuinely
shared resources — the contact budget, the margin budget — become LP rows.

**Consequence.** A solver can trade a constraint row against the objective if the
objective coefficient is large enough. It cannot trade against a candidate that is not in
the problem. Anything a solver could be tempted to violate for sufficient gain is placed
out of its reach entirely, and the rows that remain are the ones whose duals are
meaningful as shadow prices.

---

## ADR-0012 · 2026-08-22 · A second, naive constraint validator, written in M4 not M6 · Accepted

**Context.** PLAN.md M6 asks for an independent validator that the LP solution must also
satisfy. The bugs it catches are *compiler* bugs.

**Decision.** Write it in M4, alongside the compiler, sharing no code with it:
`antar/policy/validator.py` re-derives every threshold from the regulation objects and
checks solutions the slow, obvious way. A hypothesis property test asserts the two agree
on 1,000 generated candidate sets.

**Consequence.** Off-by-one errors on a threshold — `>` where the regulation says "at
least" — are found by disagreement between two implementations rather than by a test
written by the same person who wrote the bug. The validator is deliberately O(n²) and
does not care.

---

## ADR-0013 · 2026-08-22 · Gate coverage is discovered, not enumerated · Accepted

**Context.** N2 requires that every money action passes through `PolicyGate`. The obvious
test lists the executors and checks each. That test passes forever and silently stops
covering the executor added in M7 — the same failure shape as POSTMORTEM D10, where a
check kept passing while the thing it guarded drifted out from under it.

**Decision.** `tests/unit/test_gate_coverage.py` discovers its own scope. It derives the
set of money-moving client methods by introspecting `RazorpayClient` for calls that
require an idempotency key, walks `antar/act/executors/` at run time, and asserts every
function reaching one of those methods carries `@requires_gate`. A separate check asserts
no module *outside* the executors package touches them at all.

**Consequence.** An executor nobody told the test about is still covered. The decorator
also enforces the invariant at run time, so a static-analysis gap does not become a live
money path.

---

## ADR-0014 · 2026-08-23 · One `learners.py` rather than four modules · Accepted

**Context.** PLAN.md section 6 sketches `uplift/t_learner.py`, `x_learner.py`,
`r_learner.py`, `causal_forest.py`.

**Decision.** Gather them in `uplift/learners.py`. Each is 30–50 lines and the
interesting content is the *contrast* between them — which arm's data each leans on,
which needs a propensity — and that reads better on one page than across four files.

**Consequence.** A deviation from the specified layout, recorded rather than silent.
`base.py` still holds the protocol, so the extension point is unchanged.

---

## ADR-0015 · 2026-08-23 · Exploration draws treatment first, then a channel · Accepted

**Context.** `docs/SIMULATOR_CARD.md` §7 says exploration assigns "a uniformly random
intervention from the feasible set". Read literally — uniform over
`{None, SMS, WhatsApp, Email, Voice}` — that puts `P(treated)` at 0.8.

**Decision.** Draw treatment at 50/50 first, then a channel uniformly from the feasible
contacts. `P(treated) = 0.5`; `P(channel | treated) = 1/|feasible contacts|`.

**Consequence.** The propensity remains exactly known, which is the property
`docs/EVALUATION.md` §7.1.1 depends on, while the arms are balanced enough to estimate
a contrast. The literal reading produced a 74%-treated exploration split, a degenerate
Qini denominator, and a negative AUUC for every learner — POSTMORTEM D14. The simulator
card has been updated.

---

## ADR-0016 · 2026-08-23 · The pre-registered winner stands, despite a better metric · Accepted

**Context.** The bake-off revealed that negative-region sign F1 — half of the amended
selection score — is beaten by a trivial "always abstain" predictor (F1 0.261 against
the best learner's 0.254). The rupee-denominated abstention value discriminates
properly where F1 does not. Selecting on the rupee metric would change the winner from
`x_learner` to `r_learner`.

**Decision.** Keep `x_learner`. The selection rule was fixed before the numbers existed
and it is not re-opened because we now know which model it favours.

**Consequence.** We ship a model that a better criterion would not have chosen, and say
so. POSTMORTEM D15 records the methodological finding and flags it as a candidate
amendment for a future protocol, where it can be committed before the numbers exist —
which is the only order in which it would mean anything.

---

## ADR-0017 · 2026-08-23 · CI is the authority on "is the build green" · Accepted

**Context.** Twice in one session a guard fired on newly-written code and caught a real
defect — the leakage scan on `features.py`'s denylist, the inferential registry on
`specification_curve`. Once a red suite was committed anyway, because
`pytest -q | tail -4 && git commit` takes its exit status from `tail`, which always
succeeds.

Adding `python tasks.py gate` fixed that invocation. It does not fix the class: a gate
that can only be trusted when invoked one specific way will be invoked the other way
by someone under deadline pressure.

**Decision.** `.github/workflows/ci.yml` is the verdict. It runs the **same**
`tasks.py` targets a developer runs, so the two cannot drift. `main` is protected by
it. A local pass is a convenience.

Three jobs beyond lint-and-test:

- **reproducibility** runs `make evaluate` on a clean checkout and then runs it *again*,
  diffing the output. N4 is a claim about a clean checkout, so it is tested on one, and
  the second run is what would have caught D13.
- **coverage** enforces the 85% gate separately, so a coverage regression cannot be
  waved through as "the tests pass".
- A **nightly schedule**, because D13 was found by accident when a midnight rolled
  over. Time-dependent failures should be found by a cron, not by luck.

**Consequence.** The pre-push hook runs `tasks.py gate` too, so the local and remote
paths are the same command. If they ever disagree, the local one is the one that is
wrong.

---

## ADR-0018 · 2026-08-23 · Deleting two detection components on their own measurement · Accepted

**Context.** `docs/EVALUATION.md` §12.2 was committed before the allocator existed and
bound us to delete any governed component whose removal does not reduce net incremental
rupees by a margin whose CI excludes zero, with ambiguity resolving to DELETE.

**Decision.** Honour it. Measured across 3 seeds against the full L3 objective:
`downtime_crosscheck` Δ ₹0.00 (CI [0, 0]); `changepoint_detector` Δ **−₹846.76** per
1,000 cycles (CI [−2,540, 0]). Both **DELETE**. `detect.enable_*` flags are now `false`,
and `tests/statistical/test_component_retention.py` fails if the configuration and the
recorded verdict disagree.

**Consequence.** Detection got measurably worse — accuracy 92.4% → 91.7%, `ISSUER_DOWN`
recall 0.82 → 0.70, wrong-action cost ₹140k → ₹153k — and net money got better by ₹847
per 1,000. Both halves are reported (LIMITATIONS L13).

The flags live in config rather than being read from `antar/eval/retention.py`, because
`antar.detect` may not import `antar.eval`; doing so would put the ground-truth-aware
harness on the import path of a production layer. Config is the seam.

**What made this real.** POSTMORTEM D16: the first run of this measurement returned a CI
of exactly (0, 0) because L2 had no influence on the allocation at all. Deleting on that
would have been a false verdict presented as discipline.

---

## ADR-0019 · 2026-08-23 · Shadow prices are labelled by the method that produced them · Accepted

**Context.** PLAN.md M6 asks for shadow prices on contact capacity, margin budget, and
"the evening contact window". Only the first two are LP rows. `TRAI-01` is a
*feasibility filter* — candidates outside the window are removed before the solver sees
them — so it has no dual.

**Decision.** Two mechanisms, each labelled on the record. `ShadowPrice.method` is
`lp_dual` for capacity rows and `counterfactual_resolve` for filter constraints, where
the price is the objective difference from re-running with the constraint relaxed.

**Consequence.** No number is presented as a dual that is not one. The window's
counterfactual price came out at **₹0 per 1,000 cycles**, and so did the capacity dual —
because at base-scenario opt-out sensitivity Antar declines slots it is entitled to use.
That is reported as the finding it is (LIMITATIONS L14) rather than replaced with a
number from a regime we did not measure.

---

## ADR-0020 · 2026-08-23 · The phase-diagram extension is post-hoc and labelled · Accepted

**Context.** The pre-registered grid (§9.4) came back with Antar ahead in every one of
176 cells across both panels. A unanimous result locates no boundary.

**Decision.** Extend the opt-out axis below the pre-registered floor (0.005–0.035) and
label every extended cell `*` in renderings and `extended` in the artifact. The
pre-registered grid remains the headline; no claim rests on the extension without saying
so.

**Consequence.** The boundary is located at an opt-out sensitivity of ≈0.035, and the
contact capacity is shown to bind only below 0.05 — which is what turns "the shadow
price is zero" from a null result into a statement about which resource is scarce in
which regime. Extending an axis after seeing a result is legitimate; extending it
silently is not, and the distinction is the entire content of this ADR.

---

## ADR-0021 · 2026-08-24 · The model fills slots; it never writes a body · Accepted

**Context.** N1 says the LLM writes language only. That is easy to state and easy to
erode: the shortest path from "generate a message" to a working demo is to hand Claude
the customer context and print what comes back. Every regulation in
`policy/regulations.py` then depends on a model's judgement, and TRAI-03 in particular
depends on it declining to be helpful.

**Decision.** Registered templates in `act/templates/registry.yaml` own the message body.
The model is asked for exactly one slot — `reason`, a short human phrase explaining why
the payment failed — and `DraftContext.fixed_slots()` supplies every amount, date, URL,
and merchant name from the `Decision`. `Template.render()` rejects missing slots *and
extra* ones, and `_parse()` discards invented keys before rendering rather than after.

**Consequence.** A prompt injection that persuades the model to emit
`{"amount": "10000", "reason": "..."}` changes nothing: `amount` is not a slot the model
is permitted to fill, and the rendered message carries the amount the allocator decided.
The model cannot state a number, a date, or a URL, which removes the entire category of
"the LLM hallucinated a figure into a customer-facing message". The cost is that message
variety is bounded by the template registry — five templates — and that is the right
trade for money-adjacent language.

**Tested by** `test_the_model_is_never_asked_for_an_amount_a_date_or_a_url` and
`test_injection_cannot_raise_a_discount_above_the_cap`.

---

## ADR-0022 · 2026-08-24 · A rule match pins the score at 1.0 and the classifier cannot lower it · Accepted

**Context.** `contamination.py` runs two detectors. The obvious design is to combine
them — average the scores, or let a confident classifier veto a marginal rule match —
and it is the design that fails in an incident review, because the answer to "why did
this promotional message go out?" becomes "the model thought it was fine."

**Decision.** Rules run first. Any rule match returns `score=1.0`, `blocked=True`, and
the triggering rule ids, regardless of the classifier. The classifier can only *raise* a
score that no rule set. A classifier that throws is caught and treated as 0.0 —
degrading the detector rather than disabling it, because the load-bearing half has
already run.

**Consequence.** The refusal is always explainable by a named construction, and the
worst a broken or adversarially-influenced classifier can do is fail to add coverage.
It can never remove any. D19 then showed the other side of this: when *both* halves miss,
nothing catches it, and the honest accounting of how much each half actually contributes
is **LIMITATIONS L15** — pinned by a test so the table cannot drift.

**Tested by** `test_a_rule_match_outranks_a_confident_classifier`,
`test_a_broken_classifier_does_not_open_the_gate`.

---

## ADR-0023 · 2026-08-24 · A delivered message is declared irreversible, not compensated · Accepted

**Context.** `saga.py` unwinds a partially-completed workflow by walking the log
backwards. A payment link can be cancelled. A charge can be refunded. A delivered SMS
cannot be recalled, and the tempting compensation is an automatic "please ignore our
previous message".

**Decision.** `Step` accepts `compensate=None` only alongside an `irreversible_reason`;
`Saga.add()` raises otherwise, because silence is indistinguishable from an oversight.
The send step declares `IRREVERSIBLE_SEND` and `compensate_all()` reports it in
`SagaResult.irreversible`, which makes `result.clean` false. An operator decides.

**Consequence.** An auto-correction would be a second unsolicited contact, a second
`C-BUDGET` slot, and a second RBI-EM-02 opt-out prompt — a compliance cost incurred by a
compliance control, which is the failure mode this whole layer exists to avoid. The
system reports "I could not undo this" rather than reporting a clean unwind it did not
achieve. That distinction is the same one ADR-0019 makes about shadow prices and
LIMITATIONS L14 makes about zero: say what happened, not what would sound finished.

---

## ADR-0024 · 2026-08-24 · The chain link covers the whole entry, not just the payload · Accepted

**Context.** PLAN.md M8 specifies the ledger as hash-chained on `prev_hash` +
`payload_hash`. Implemented literally — entry *n* storing `sha256(payload of n-1)` —
that detects an edited payload and misses everything else. Relabel an `ACTION` as an
`ALERT`, or move a timestamp six hours, and every payload hash still agrees.

**Decision.** `entry_hash` covers `seq`, `prev_hash`, `payload_hash`, `kind`, and
`written_at`, joined with a delimiter that cannot occur in any of them; entry *n+1*
links to that. Separately, `verify_chain()` asserts `seq` is contiguous from 1, because
a chain of hashes detects a modified entry but a verifier walking `ORDER BY seq` accepts
a chain with a hole in it.

**Consequence.** An edit anywhere breaks the chain at the point of the edit, which is
where an auditor needs to be shown. One thing remains undetectable from inside the
ledger — **truncating the tail** leaves a shorter chain that verifies perfectly — and
`test_truncating_the_tail_is_not_detected_by_the_chain_alone` records that as a stated
negative result rather than an oversight. `head()` exists so it can be published, which
is the only defence against it.

---

## ADR-0025 · 2026-08-24 · Replay compares behaviour, not bits, and never produces an outcome · Accepted

**Context.** "Replay a batch deterministically" admits two readings. One re-runs history
to see whether today's code decides the same way. The other re-runs it to get a better
answer.

**Decision.** Only the first. `replay()` compares a fixed field set — channel, schedule,
discount, arm, binding constraints, stopping rule, plus floats with a tolerance —
between the recorded decision and a freshly computed one. `decision_id` is deliberately
excluded: it is derived from the versions (ADR-0005), so comparing it would report a
divergence on every version bump and bury the real ones. `ReplayResult` has no field for
an outcome, and cannot acquire one by accident.

**Consequence.** A refactor is checkable. A counterfactual is not available here and
must go through `eval/policies.py`, which is explicit about being an estimate. Every
replayed decision runs under a `FrozenClock` pinned to the recorded time (D13), and a
ledger that does not verify is refused rather than replayed — a confident comparison
against an altered record is worse than no comparison.

---

## ADR-0026 · 2026-08-24 · The production path values candidates from models only · Accepted

**Context.** `eval/policies.py` values candidates from the simulator's response model.
That is correct for the three-policy comparison: valuing every policy on the same oracle
is what makes it fair. Reusing it in the pipeline that writes the ledger made the audit
record attribute the simulator's answer key to L3 (POSTMORTEM D22).

**Decision.** The two paths share feasibility and nothing else. `pipeline._estimate_values`
computes `recovery_uplift x amount - channel cost - optout_uplift x amount x multiplier`
from two fitted models. Ground truth enters the pipeline at exactly one point,
`_realise`, which draws the simulated outcome and is labelled `"simulated": true`.

**Consequence.** Antar contacts more and induces more opt-outs than the oracle policy did
— 12 → 29 contacts and 0 → 5 opt-outs on a 400-event slice — because the oracle policy
was cheating and this one is not. Two further consequences are recorded as limitations
rather than smoothed over: channel choice falls back to cost (L16), and the opt-out term
is a causal effect only because the simulator gives the untreated arm a rate of exactly
zero (L17).

---

## ADR-0027 · 2026-08-24 · No fitted model means no action, not a naive one · Accepted

**Context.** PLAN.md section 10: *"Uplift model missing / version mismatch → refuses to
act; does not silently fall back to targeting everyone."* An earlier `run_pipeline`
returned `None` for an unfitted model and carried on, recording `uplift_estimate=0.0`.
With every candidate scored identically and a capacity constraint to fill, that policy
*is* "contact everyone until the budget runs out" — the naive thing, arrived at by
accident.

**Decision.** `_fit_models` raises `NoModel` rather than returning a degraded object.
A caller cannot carry on, because carrying on is not expressible.

**Consequence.** A batch with too little exploration data produces no contacts and a
loud failure. That is the correct behaviour for a system that moves money: the safe
failure is do nothing.

---

## ADR-0028 · 2026-08-25 · Both arms of an ablation are constructed, never inherited · Accepted

**Context.** `retention_across_seeds` built the *with-component* arm from the shipped
config. That was correct in M6 — and became vacuous the moment M6's own verdict switched
those components off, because both arms then described the same detector and every delta
was exactly zero (POSTMORTEM D25). The pre-registered rule read zero as ambiguity and
resolved to DELETE, so a deleted component could never be reconsidered. The rule had
become a ratchet.

**Decision.** Both arms are constructed explicitly from an override. Neither inherits the
current default. The measurement additionally raises if the two arms produce identical
results on every seed.

**Consequence.** The ablation measures the component rather than the build, and a
degenerate result is a loud failure rather than a quiet verdict. Re-run after the fix, the
verdicts are unchanged — both still DELETE — but now on **positive evidence** (removing
each *improves* net by ₹284 and ₹243 per 1,000 at-risk cycles) rather than on an interval
of zero width. The same finding, honestly
obtained, which is the only version worth having.

---

## ADR-0029 · 2026-08-25 · Derived numbers are checked against their own components · Accepted

**Context.** D24: an artifact key named `antar_minus_propensity_per_1000_rupees` held the
changepoint detector's retention effect, in paise, for two milestones. Every safeguard in
this project points at the *inputs* to numbers — no leakage, no answer key, pinned clocks,
verified regulations. Nothing pointed at the outputs.

**Decision.** `tests/statistical/test_artifacts_are_self_consistent.py` asserts that every
derived figure in an artifact is recomputable from that artifact's own parts, and
`tests/statistical/test_results_are_reproducible.py` asserts that every number in the
README appears in some artifact.

**Consequence.** The two together give N4 teeth: provenance from the second, meaning from
the first. Neither is sufficient alone — a number can appear in an artifact under a
completely different meaning, which is precisely what D24 was. Both were written *against
the stale artifacts* and both failed immediately, which is the only evidence that a guard
works.
