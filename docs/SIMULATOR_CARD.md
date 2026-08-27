# SIMULATOR CARD — Antar Recovery Simulator

**Status:** Written before implementation (22 Aug 2026). Parameters marked
`[MEASURE @ M2]` are placeholders to be filled by a committed script, never by hand.

**Read this first if you are evaluating Antar's results.** Everything Antar claims about money
recovered is produced inside this simulator. This document exists so you can decide how much of
it to believe, and so you can find the weak points faster than we could hide them.

---

## 1. Purpose and scope

### 1.1 What this is

A deterministic, event-sourced generator of at-risk recurring-payment cycles for an Indian
merchant, with **known ground-truth treatment effects**. It exists so that Antar's causal
machinery can be validated against an answer we control.

### 1.2 What this is not

- **It is not a forecast.** Recovery rates produced here are properties of the parameters we
  chose. They are not estimates of what any real merchant would see.
- **It is not calibrated to Razorpay's production data.** We have no access to it.
- **It is not a claim about Indian consumer behaviour.** The behavioural parameters are
  author-chosen, informed by mechanism rather than measurement.

### 1.3 The one sentence that governs every artifact

> Every recovery number Antar reports is a simulated number. Where a figure appears in the
> README, the console, or the pitch video, the words "in simulation" must appear with it.

If you find a place where that rule is broken, it is a bug — file it.

---

## 2. Why a simulator at all

We considered three alternatives and rejected each for a stated reason. This is recorded here so
the choice is legible rather than convenient.

| Option | Why rejected |
|---|---|
| Public payment-failure datasets | The available ones are US card-fraud datasets (IEEE-CIS, Sparkov and similar). Wrong geography, wrong failure taxonomy, wrong regulatory regime, and no recurring-mandate structure at all. Building on them would mean defending a distribution mismatch through the entire evaluation. |
| Razorpay test-mode traffic alone | Test mode produces real API artifacts but no customer behaviour. There is nobody on the other end to respond to a dunning message, so there is no outcome to measure. |
| Historical merchant data via a partner | No access, and no route to it inside the buildathon window. |
| **Simulator with known ground truth** | **Chosen.** It is the only option where the counterfactual — what would have happened without intervention — is knowable, which is the entire subject of this project. |

**The honest cost of this choice:** external validity is zero for the *magnitudes* and unknown
for the *rankings*. What survives is the machinery: whether the estimators recover effects we
planted, whether the constraint solver respects the regulations, whether the system degrades
safely. Those are the claims we make. See §11.

---

## 3. What is generated

### 3.1 Entities

| Entity | Volume (base scenario, seed 20260822) | Notes |
|---|---|---|
| Merchants | 4 | OTT, education, insurance, mutual fund. Categories drive the `RBI-EM-04` AFA threshold |
| Customers | 2,000 | Each carries a hidden latent vector (§4) |
| Subscriptions / mandates | 2,000 | One per customer; monthly billing |
| Billing cycles | 7,838 attempts | 4 cycles per mandate over a simulated 120 days, truncated by revocation |
| At-risk events | 3,436 | 1,936 mandate failures + 1,500 checkout abandonments |
| First-attempt failure rate | 24.7% | **Invented.** A property of `base_failure_rate` and the balance curve |
| Checkout-abandonment events | 1,500 | Second loss class, T1 tier |
| Issuer × method segments | 12 | 4 synthetic issuers × 3 methods (UPI AutoPay, card, eNACH) |
| Downtime windows | 354 | Injected per §4.5; ~3% of attempts land inside one |
| Events above the AFA ceiling | 15.3% | Both sides of the `RBI-EM-03`/`-04` boundary carry real mass |
| Events with an ambiguous error code | 24.9% | The share the taxonomy table alone cannot resolve — see §4.2 |

True-cause mix for mandate failures (base): `INSUFFICIENT_FUNDS` 40.5%,
`TECHNICAL_DECLINE` 18.3%, `AFA_REQUIRED` 13.1%, `ISSUER_DOWN` 12.9%,
`RISK_DECLINE` 8.4%, `MANDATE_REVOKED` 6.9%. **All invented** — §12.5.

**These are regenerated, never typed.** `python tasks.py calibration-report` writes
`artifacts/calibration.md` and `artifacts/calibration.json`; the table above is a
transcription of that output and `scripts/make_figures.py` reproduces it.

Volumes are set so that the 20% control holdout has adequate power for the primary metric. The
power analysis lives in `docs/EVALUATION.md` and must be committed **before** the final run.

### 3.2 Simulated clock

120 simulated days, 1-minute resolution. All timestamps are IST. There is exactly one
authoritative clock; no component may call `datetime.now()`. This is enforced by
`tests/unit/test_clock.py::test_no_module_outside_clock_reads_the_wall_clock`, which
parses every module in the package and fails on any wall-clock call.

**Correction to the pre-implementation draft:** the clock lives at `antar.clock`, not
`antar.simulator.clock`. The policy and decision layers need it for the `C-LEAD` and
`C-WINDOW` constraints, and importing it from the simulator would have put an
answer-key package on the import path of every production layer — exactly what
`tests/statistical/test_no_leakage.py` forbids. IST is modelled as a fixed +05:30
offset (ADR-0004).

---

## 4. Generative process

Every parameter below is an **author choice**. Where a choice is anchored to something external,
the anchor is named. Where it is invented, it says so. Do not let this table drift out of sync
with `antar/simulator/latents.py` — a test asserts they match.

### 4.1 Customer latent vector

Each customer draws a hidden vector, never visible to any Antar layer:

| Latent | Distribution | Meaning | Anchored? |
|---|---|---|---|
| `p_self_heal_base` | Beta(α, β), `[MEASURE @ M2]` | Probability the mandate succeeds on the next scheduled cycle with no intervention | **Invented.** This is the single most consequential parameter in the whole system — see §12.1 |
| `salary_day` | Categorical over {1, 5, 7, 10, 25, 30} | Day of month when balance peaks | Weakly anchored: Indian salary-credit dates cluster at month start and month end |
| `balance_curve` | Exponential decay from `salary_day` with per-customer half-life ~ LogNormal | Funds availability over the month | **Invented**, but mechanistically motivated |
| `channel_response` | Dirichlet over {SMS, WhatsApp, voice, email} | Relative responsiveness by channel | **Invented** |
| `optout_sensitivity` | Beta(α, β), `[MEASURE @ M2]` | Propensity to use the RBI-mandated opt-out when prompted | **Invented.** See §6 — this is the second most consequential parameter |
| `persuadability` | Beta(α, β), see `artifacts/calibration.md` | **Ceiling** on P(persuaded) under the best channel, best timing, first attempt | **Invented.** The ceiling is enforced: every modifier in `persuasion_probability` is confined to (0, 1], asserted by `test_persuadability_is_actually_the_ceiling`. It was not, once — see POSTMORTEM D1 |
| `price_sensitivity` | LogNormal | Response to discount size | **Invented** |
| `tenure_months` | Geometric | Months subscribed | **Invented**, and the one latent that is **observable by design** — a merchant reads it from their own subscription records. It is therefore permitted to reach a feature builder, via a single-entry allowlist in `tests/statistical/test_no_leakage.py`, and the customer record must carry the identical value (POSTMORTEM D7) |
| `intent_to_churn` | Bernoulli(p), `[MEASURE @ M2]` | Latent desire to cancel, independent of payment failure | **Invented.** Drives the sleeping-dogs population — see §6.3 |

### 4.2 Failure generation

A cycle fails with probability determined by:

```
p_fail = f(balance_at_debit_time, segment_health, amount, afa_required, mandate_state)
```

Given failure, a `FailureClass` is drawn from a segment-conditional categorical distribution:

| Class | Base share | Source of the class labels |
|---|---|---|
| `INSUFFICIENT_FUNDS` | `[MEASURE @ M2]` | Razorpay's published error taxonomy |
| `ISSUER_DOWN` | Driven by injected downtime, not a fixed share | Razorpay Downtime API semantics |
| `TECHNICAL_DECLINE` | `[MEASURE @ M2]` | Razorpay error taxonomy |
| `RISK_DECLINE` | `[MEASURE @ M2]` | Razorpay error taxonomy |
| `AFA_REQUIRED` | 65% of above-ceiling failures | `RBI-EM-03` / `RBI-EM-04` |
| `MANDATE_REVOKED` | `[MEASURE @ M2]` | Razorpay subscription lifecycle |

**The class labels and their `error_code` / `error_reason` / `error_source` / `error_step`
payloads are taken from Razorpay's documented error taxonomy.** The *shares* are invented. That
distinction matters: our detector is being tested against realistic label structure with
unrealistic frequencies.

**Emission overlaps on purpose.** `antar/simulator/failure_emission.py` maps each true cause to
a *distribution* over error reasons, and those distributions overlap:
`gateway_technical_error` is emitted by both `ISSUER_DOWN` and `TECHNICAL_DECLINE`,
`declined_by_issuer` by both `INSUFFICIENT_FUNDS` and `RISK_DECLINE`, and `payment_failed` by
everything. **24.9% of generated events carry a code the lookup table cannot resolve.**

This is the anti-circularity guard for the *detection* result, and it matters as much as the
one for the uplift result. If the generator emitted a unique code per cause, a lookup table
would score 100% and the classifier, the downtime cross-check, and the changepoint detector
would all be decoration. `failure_emission.py` was written independently of
`antar/detect/taxonomy.py`, and `tests/statistical/test_taxonomy_realism.py` asserts the
overlap survives.

For the same reason `AFA_REQUIRED` is 65% of above-ceiling failures rather than 100%: a
₹40,000 debit can still bounce for want of funds, and a deterministic rule would have let the
detector infer the label from the amount alone and post a per-class recall of 1.00 that means
nothing.

### 4.3 Amount distribution

LogNormal, truncated, parameterised so that a meaningful fraction of cycles land above the
₹15,000 AFA threshold and — for the insurance/mutual-fund/credit-card merchant profiles — a
smaller fraction approaches ₹1,00,000. This is deliberate: the AFA boundary is a decision-relevant
discontinuity and the evaluation must exercise both sides of it.

### 4.4 Segment health

Each issuer × method segment has a latent health process (a two-state Markov chain,
`HEALTHY` ↔ `DEGRADED`) with configurable transition rates. Degradation raises `p_fail` for that
segment. This is what the EWMA/CUSUM detector in `antar/detect/changepoint.py` must find.

### 4.5 Downtime injection

Downtime windows are injected with severity in {`low`, `medium`, `high`}, affecting method,
issuer, and PSP, matching the shape of the Razorpay Downtime API's entity. High-severity windows
produce near-total failure for the affected segment.

**Why this matters for the thesis:** during a downtime window, contacting the customer is pure
waste — they cannot pay, the failure is not theirs, and under `RBI-EM-01` a retry costs a
24-hour notification. A system that cannot separate `ISSUER_DOWN` from `INSUFFICIENT_FUNDS`
burns budget and goodwill. This is a primary reason the detection layer exists.

---

## 5. Response model

`response_model.py` samples the ground-truth outcome for a (customer, cycle, intervention) triple.

### 5.1 Structure

```
P(recover | intervention) = P(self_heal) 
                          + (1 - P(self_heal)) × P(persuaded | intervention)
                          - P(optout_induced | intervention)
```

The middle term is the only part an intervention can influence upward. The last term is the harm
it can cause. **Incremental effect is the difference between this and the same expression with
`intervention = NONE`** — which is exactly the quantity the uplift estimators must recover.

**Implementation note.** The code uses the exact factorisation

```
P(recover | intervention) = (1 - P(optout_induced)) x [ P(self_heal)
                                                     + (1 - P(self_heal)) x P(persuaded) ]
```

of which the expression above is the first-order expansion. An opt-out revokes the mandate, so
recovery is conditional on it not happening rather than merely reduced by its probability. The
two agree to first order and the exact form cannot produce a negative probability.

### 5.2 Where the treatment effect comes from

`P(persuaded | intervention)` is a function of:

- `channel_response[channel]` — right channel for this person
- timing alignment with `balance_curve` — is there money in the account when we ask?
- `price_sensitivity × discount_size` — if a discount is offered
- attempt number — diminishing returns, then annoyance
- `tenure_months` — longer-tenured customers are more persuadable

None of these is a constant treatment effect. **Heterogeneity is built in on purpose**, because a
constant effect would make the uplift models pointless and the evaluation vacuous.

### 5.3 The constant-effect validation mode

A special mode, `scenarios.constant_effect`, injects a known constant treatment effect with all
heterogeneity switched off. The M2 acceptance test requires that a naive difference-in-means
recovers that constant within its confidence interval. **If this fails, nothing downstream is
trustworthy and the build stops.**

---

## 6. The opt-out hazard — read this section carefully

This is the mechanism that produces Antar's headline finding, so it is also the mechanism most
likely to be dismissed as circular. Here is exactly how it works and exactly why we think it
isn't planted.

### 6.1 The regulatory mechanism

Under the RBI's *Digital Payments — E-mandate Framework, 2026*, a pre-transaction notification
must be sent at least 24 hours before each debit, and it must carry a facility to opt out of that
transaction or the mandate. **The notification is therefore also a cancellation prompt.** This is
not our invention; it is what the framework requires. (Citation in
`docs/REGULATORY_REGISTER.md`; verify against the primary circular before submission.)

### 6.2 How it is implemented

```
P(optout_induced | notification) = g(optout_sensitivity, 
                                     intent_to_churn, 
                                     notifications_received_recently)
```

The hazard is a **function of the notification event**, not a property attached to a
pre-labelled segment. There is no `is_sleeping_dog` flag anywhere in the simulator. A customer
becomes a sleeping dog *emergently*, when their `intent_to_churn` and `optout_sensitivity` are
high enough that the expected opt-out loss exceeds the expected persuasion gain.

### 6.3 The anti-circularity test (M2 acceptance criterion)

A negative-uplift population must emerge in **at least two of the three scenarios** without
scenario-specific tuning. Concretely, the test:

1. Runs `base`, `conservative`, and `aggressive` with parameters set independently of this test
2. Computes ground-truth per-customer uplift directly from the response model
3. Asserts a non-trivial mass of customers has ground-truth uplift < 0 in ≥2 scenarios
4. Asserts that no simulator parameter is named or tuned per-scenario to force this

The test lives at `tests/statistical/test_anti_circularity.py` and is deliberately named so a
reviewer browsing the repo finds it. The gate itself lives in `antar/eval/claims.py`, because
§10's consequence for this test is a statement about the *artifacts* rather than the test
runner: `make evaluate` writes `artifacts/claims.json`, and the README refuses to state a
claim the verdict does not support.

### 6.3.1 The measured result — the claim this section was written to support is WITHDRAWN

| Scenario | Share of customers whose **best available action** still has negative uplift |
|---|---|
| `conservative` | **23.09%** — clears the 5% bar comfortably |
| `base` | **2.67%** — does *not* clear it, ranging 2.53–2.93% over three seeds |
| `aggressive` | **0.02%** — effectively none |

**One scenario of three. The pre-registered rule in §6.3 requires two.** Per §10 the
consequence is stated in the table itself: the sleeping-dogs finding is withdrawn from
all artifacts. `artifacts/claims.json` records the verdict, and
`tests/statistical/test_withdrawn_claims_are_not_stated.py` fails the build if any
document asserts the claim anyway — or if the README drops it silently, which is the
more tempting failure.

The numbers above are lower than the ones this card carried until POSTMORTEM D38. The
response model discounted the *treated* arm by its opt-out hazard and discounted the
untreated arm by nothing, so every customer's treated outcome was penalised against an
unpenalised counterfactual and the negative-uplift population was inflated by
construction. The asymmetry had been logged as known-and-unfixed on the reasoning that
correcting it "would move numbers in Antar's favour" — **backwards**, and that inverted
note is what protected the defect across three review cycles.

Measured over three seeds with the scan pinned to a fixed instant
(`claims.SCAN_REFERENCE`). It was not always pinned — see POSTMORTEM D13, where the base
figure moved across a midnight and crossed the threshold on its own.

### 6.3.2 The specification curve — the finding does not survive it either

Our own reported figure moved 5.13% → 4.67% → 5.83% during this build, from analytic
choices alone. Rather than apologise for each move, `docs/EVALUATION.md` §9.5
pre-registered a sweep over **every** defensible analytic choice. All 540 of them were run.

| | |
|---|---|
| Specifications | **540** (6 reference instants × 5 seeds × 3 measurement windows × 3 definitions of "negative" × 2 action sets) |
| Median share | **4.33%** (IQR 2.33%–9.38%, range 0.33%–19.50%) |
| **Clearing the 5% bar** | **46%** of specifications |
| Pre-registered specification | 2.83%, at the **28.7th percentile** of its own distribution |

**The base-scenario result is a coin flip, and it lost.** Fewer than half the analytic
choices we could defensibly have made clear the bar, and the majority rule in §9.5 is not
satisfied. Before D38 this table read 9.50% median and 83% clearing, and this paragraph
said the opposite; both are corrected here rather than deleted, because the movement is
the finding.

**We did not pick a flattering specification.** The pre-registered choice sits at the
28.7th percentile of the distribution it generated — less favourable to our own claim
than 71% of the alternatives. That is not a virtue we planned; it falls out of having
chosen `best_available` (maximise over channels, most generous to treatment) and the
`strict` definition before seeing any of this. It is also the reason the claim failed: a
specification chosen to be unflattering was unflattering.

Which choices actually moved the answer, most to least:

| Dimension | Spread (sd of level means) | What it says |
|---|---|---|
| **Action set** | 0.037 | `best_available` 2.70% vs `reference_sms` **10.03%**. Much the largest effect. A merchant with one SMS integration — i.e. most merchants — sees roughly four times the harmed population we report, and would clear the bar on this axis alone |
| Definition of "negative" | 0.022 | strict 8.76% → margin 6.92% → conservative_ci 3.42%. Monotone, as it must be |
| Reference instant | 0.007 | 5.60%–7.68%. The D13 axis. Real but modest once pinned |
| Seed | 0.002 | 6.16%–6.76%. Sampling noise is **not** what moved our headline |
| Measurement window | 0.002 | 6.22%–6.57%. Essentially irrelevant |

The last two rows matter for the postmortem: the 1.2-point wobble during the build was
**not** seed noise. It was a bug (D6) and an unpinned instant (D13), both now fixed and
both now guarded.

Regenerate with `python tasks.py evaluate`; the artifact is
`artifacts/specification_curve_base.json`.

Three things a reader should take from this rather than from the headline:

1. **The claim is withdrawn, and what remains is narrower.** Under the registered action
   set the negative-uplift population is not large enough to report. It is large under
   `reference_sms` and under the `conservative` scenario, which is a statement about
   *which merchants* rather than a finding about customers in general, and it is not
   what this project set out to claim.
2. **The gate did not pass on the first run.** It failed, five genuine defects were found
   and fixed, and it then passed — and then D38 unwound the pass. The full
   before-and-after, including which fixes moved the result toward the finding and which
   moved it away, is disclosed at the top of
   `tests/statistical/test_anti_circularity.py` and in `docs/POSTMORTEM.md` D1–D3, D6,
   D13 and D38. That ordering is uncomfortable and is published rather than buried.
3. **A threshold this soft should never have been the headline.** `docs/EVALUATION.md`
   §9.4 pre-registers a phase diagram over `mean_self_heal` × `mean_optout_sensitivity`,
   reporting the boundary where uplift allocation stops beating propensity targeting. A
   boundary is a statement about mechanism; a threshold crossing is not. The result the
   project reports is the one in §15 and in the README, which needs no assumption about
   what a cancellation costs and did not depend on this claim.

### 6.4 The honest caveat

**We chose the functional form of `g` and the distribution of `optout_sensitivity`.** A different
choice would produce a different sleeping-dogs population, possibly none at all. What we can
defend is: (a) the mechanism is real and regulatorily mandated, (b) the finding is not
hard-coded, and (c) we report the sensitivity across three parameterisations rather than one.

What we cannot defend is the *magnitude*. If the panel asks "how big is this effect in reality?",
the correct answer is "unknown — this simulator cannot tell you, and here is what data would."

---

## 7. Randomised exploration and propensity logging

From the first generated batch onward:

- A configurable fraction ε (default 0.20) of at-risk events receive a **uniformly random**
  intervention from the feasible set.
- The **propensity** of every assigned action is logged with the event.
- The remaining events receive the policy's chosen action.

This is not optional and cannot be retrofitted. Without logged propensities the uplift estimates
are confounded and the doubly-robust off-policy evaluation in `antar/decide/ope.py` is
unusable. If you are reading this because exploration was skipped: the batches must be
regenerated, not patched.

The 20% randomised **control** holdout (§ `docs/EVALUATION.md`) is separate from and orthogonal
to ε-exploration. Control events receive no intervention at all and never enter any training set.

---

## 8. Scenarios

Three named parameterisations, defined in `antar/simulator/scenarios.py`. Every headline result
is reported across all three.

| Parameter | `conservative` | `base` | `aggressive` |
|---|---|---|---|
| Mean `p_self_heal_base` | High | Medium | Low |
| Mean `optout_sensitivity` | High | Medium | Low |
| Downtime frequency | High | Medium | Low |
| Effect heterogeneity | High | Medium | Low |
| Expected difficulty for Antar | **Hardest** | Reference | Easiest |

Exact values: `[MEASURE @ M2]` — filled from `scenarios.py`, generated by
`scripts/make_figures.py`, never typed by hand.

**`conservative` is the scenario that can embarrass us**, and that is the point of including it.
High self-heal plus high opt-out sensitivity means most apparent recovery is spurious and most
outreach is harmful — the regime where a naive contact-everyone policy loses money outright.
If Antar's advantage disappears here, we report that. It would be a genuine finding about when
this class of system is not worth building.

---

## 9. Calibration

### 9.1 What we could calibrate against

- **Failure-class label structure and payload shape:** Razorpay's published error taxonomy and
  webhook payload samples. `calibration.py` produces a report comparing generated
  `error_code` / `error_reason` combinations against the documented set, and flags any generated
  value that does not appear in the real taxonomy.
- **Downtime entity structure:** severity values, affected-method semantics, and the
  scheduled/unscheduled distinction from the Downtime API docs.
- **Subscription lifecycle states:** drawn from Razorpay's documented subscription states, so the
  mandate FSM is exercised against real state names and legal transitions.
- **Regulatory thresholds:** ₹15,000 / ₹1,00,000 AFA boundaries, the 24-hour notification lead
  time, the 09:00–21:00 contact window. These are not invented and are cited.

### 9.2 What we could not calibrate against

- Actual failure **rates** by issuer, method, or segment
- Actual **self-healing** rates
- Actual **response rates** to dunning by channel
- Actual **opt-out rates** following pre-debit notifications
- Actual **downtime frequency and duration** in production
- Any real customer behaviour whatsoever

**All frequencies in this simulator are invented.** The structure is real; the numbers are not.

### 9.3 What we would do with real data

Named here because the panel will ask. In priority order:

1. Fit `p_self_heal` from historical no-intervention cycles — the single highest-value input
2. Fit `optout_sensitivity` from observed post-notification cancellation rates
3. Replace the invented failure-class shares with observed segment-conditional frequencies
4. Re-run the entire evaluation and report how much the conclusions moved

Item 4 is the important one. If the ranking of the three policies is stable across our three
synthetic scenarios *and* real-data calibration, the finding is robust. Until then it is a
hypothesis with a working measurement apparatus attached.

---

## 10. Validation tests

All live in `tests/statistical/` and gate M2.

| Test | Asserts | Consequence if it fails |
|---|---|---|
| `test_determinism` | Fixed seed → byte-identical event stream | Build stops; nondeterminism invalidates every downstream comparison |
| `test_ground_truth_recoverability` | Constant-effect mode: difference-in-means recovers the planted effect within CI | Build stops |
| `test_anti_circularity` | Negative-uplift population emerges unforced in ≥2 scenarios (§6.3) | Sleeping-dogs finding is withdrawn from all artifacts |
| `test_taxonomy_realism` | Every generated `error_code` exists in the documented Razorpay taxonomy | Fix the generator |
| `test_control_isolation` | No control-group `event_id` appears in any training set | Build stops; the primary metric is compromised |
| `test_propensity_logging` | Every event has a logged propensity in (0, 1] | Regenerate batches |
| `test_no_leakage` | No latent variable is reachable from any feature used by any Antar layer | Build stops; this is the classic uplift-project failure |

`test_no_leakage` deserves emphasis. The latents are the answer key. If any of them leaks into
`antar/decide/features.py` — even indirectly, through a derived column — the uplift models will
look spectacular and mean nothing. This exact failure was caught in a prior project of ours by a
similar test; it is cheap insurance against an expensive humiliation.

---

## 11. What this simulator does not model

Stated plainly, because the omissions are where a reviewer will probe.

**Behavioural**
- Word-of-mouth, social influence, or any cross-customer effect
- Customer service contact outside the recovery channel
- Competitor switching
- Seasonality beyond the monthly salary cycle
- Festival-period spending shifts (Diwali, etc.) — a real and material effect in India
- Language preference and its effect on message response
- Learning: customers do not adapt to repeated dunning over long horizons

**Financial and operational**
- Genuine credit deterioration versus temporary liquidity shortfall
- Partial payments and payment plans
- Chargebacks and disputes arising from recovery actions
- Refund and reversal flows
- Merchant-side inventory or fulfilment constraints
- Multi-mandate customers (one customer, several subscriptions)

**Technical and regulatory**
- Real network latency, real API rate limits, real settlement timing
- Bank-side retry logic operating independently of ours
- DLT template approval delays
- Regulatory change mid-horizon
- Fraud and abuse (deliberately excluded — that is Track 02)

**Statistical**
- Unobserved confounders. There are none, by construction. This is the largest gap between the
  simulator and reality, and it flatters every causal method we test, including ours.

That last item is the most important line in this document. In a real deployment, unmeasured
confounding is the dominant threat to any uplift estimate. Our simulator cannot exercise it,
so our validation says nothing about robustness to it. Anyone extending this work should add a
hidden-confounder mode before trusting the estimates on real data.

---

## 12. Self-critique — how we would attack this

Written to be found. If a reviewer's objection is already here with a response, we have done our
job; if their objection is not here, we want to know it.

### 12.1 "Your self-heal rate is the whole result"

Correct, and it is invented. If `p_self_heal_base` is low, gross and incremental recovery
converge and the thesis loses most of its force; if it is high, the gap is dramatic. We mitigate
by varying it across all three scenarios and reporting the sensitivity table rather than a single
number. We do not claim to know its real value. **This is the weakest link in the project and we
would rather name it than have it found.**

### 12.2 "The sleeping-dogs finding is planted"

See §6. The mechanism is regulatorily mandated rather than invented, there is no segment flag,
and `test_anti_circularity` checks that the population emerges unforced across scenarios. We
concede the magnitude is a parameter choice.

### 12.3 "A simulator with no confounding makes causal inference easy"

Also correct. Our claim is about the *machinery* — that the estimators recover known effects,
that the allocator respects the constraints, that the system fails safely — not that causal
inference on real payment data is solved. See §11, final item.

### 12.4 "You tuned parameters until the result looked good"

The defence is procedural, and it only works if it is followed:
- Scenario parameters are committed **before** the final evaluation run
- The evaluation protocol is pre-registered in `docs/EVALUATION.md` and committed before the
  bake-off
- Git history shows the ordering
- All three scenarios are reported, including the one designed to be hardest

If the git history does not show that ordering, this defence is void. Do not break it for
convenience late in the build.

### 12.5 "Your failure-class shares are made up"

Yes. The labels and payload structure are real; the frequencies are not. The detection metrics
are therefore measured against realistic label structure with unrealistic prevalence, which
mainly affects the class-imbalance profile. We report per-class precision and recall rather than
a single accuracy figure specifically so this is visible.

---

## 13. Reproduction

```bash
make simulate SCENARIO=base SEED=20260822
make simulate SCENARIO=conservative SEED=20260822
make simulate SCENARIO=aggressive SEED=20260822
make calibration-report
make evaluate
```

Every figure and every number in the README, the console, and the pitch video is produced by
these commands from a clean checkout. Nothing is typed by hand. If you find a number that these
commands do not reproduce, it is a defect — report it.

---

## 14. Changelog

| Date | Change | Author |
|---|---|---|
| 2026-08-22 | Initial card written before implementation. All numeric parameters are placeholders. | — |
| 2026-08-22 | M2 implementation. Clock path corrected to `antar.clock` (§3.2). Exact factorisation of the response model documented (§5.1). `persuadability` added to the latent table (§4.1). `[MEASURE @ M2]` placeholders in §3.1 filled from `artifacts/calibration.md`. | — |
| 2026-08-22 | Merchant mix widened from 3 to 4 profiles, adding an education category whose ticket sizes straddle ₹15,000. The original mix put only 0.4% of events above the AFA ceiling, so the `RBI-EM-03`/`-04` discontinuity was never exercised; it is now 15.5%. | — |
| 2026-08-22 | Downtime frequency raised from 6 to 22 windows per 100 days per segment. At the original rate `ISSUER_DOWN` was 2% of failures, too rare for per-class recall to mean anything and too rare to exercise the "wait, do not spend a notification" decision that L2 exists for. Now 12.8%. Still invented. | — |
| 2026-08-22 | **POSTMORTEM D1:** persuasion multipliers confined to (0, 1] so `persuadability` is genuinely the ceiling it is documented as. Previously 69% of customers exceeded it and the median dunning SMS persuaded 43% of recipients. | — |
| 2026-08-22 | **POSTMORTEM D2:** opt-out hazard now divides by a fixed `REFERENCE_OPTOUT_SENSITIVITY` rather than by `scenario.mean_optout_sensitivity`, which had been cancelling the scenario axis exactly and making §8's opt-out row inert. | — |
| 2026-08-22 | **POSTMORTEM D3:** `prior_notifications` now defaults to 1, not 0. Under RBI-EM-01 every failed debit was already preceded by one notification, so the hazard had been understated on every event. | — |
| 2026-08-22 | **POSTMORTEM D6:** the anti-circularity scan's "balance peak" was computed as `replace(day=min(salary_day, 28))`, which for customers paid on the 29th or 30th is the trough. Replaced with a search (`CustomerLatents.next_balance_peak`). Moved every scenario's negative-uplift share **down**. | — |
| 2026-08-22 | **POSTMORTEM D7:** `CustomerContext.tenure_months` now comes from the latents instead of a second independent draw. The two had been uncorrelated, silently turning the only observable driver of persuasion into noise. §4.1 updated to mark tenure observable-by-design. | — |
| 2026-08-22 | §6.3.1 added: the measured negative-uplift shares, with the marginality of the base result and the fact that the gate initially failed both stated in the card rather than only in the test. | — |
| 2026-08-28 | **POSTMORTEM D38:** the response model discounted the treated arm by the opt-out hazard and the untreated arm by nothing. Corrected to `p_recover_untreated = (1 - p_optout_baseline) x p_self_heal`. Negative-uplift shares fell to conservative 23.09% / base 2.67% / aggressive 0.02%, specification clearance from 83% to 46%, and **the sleeping-dogs claim was withdrawn** under the §6.3 rule. §6.3.1 and §6.3.2 rewritten to the corrected values. | — |

*(Append an entry for every parameter change, and never edit a previous entry.)*

---

## 15. The phase diagram — where this class of system pays for itself

`docs/EVALUATION.md` §9.4, pre-registered before any cell was computed.
176 cells: 11 self-heal levels × 8 opt-out levels ×
2 channel-mix panels, each a full generate → detect → allocate cycle. Metric: **net
expected rupees per 1,000 at-risk cycles, P3 (Antar) minus P2 (propensity targeting)**.

### The map (`best_available` panel)

`+` = Antar ahead of propensity targeting in that cell.

```
        self-heal ->
         0.1 0.15  0.2 0.25  0.3 0.35  0.4 0.45  0.5 0.55  0.6
 0.05     +    +    +    +    +    +    +    +    +    +    +
 0.1      +    +    +    +    +    +    +    +    +    +    +
 0.15     +    +    +    +    +    +    +    +    +    +    +
 0.2      +    +    +    +    +    +    +    +    +    +    +
 0.25     +    +    +    +    +    +    +    +    +    +    +
 0.3      +    +    +    +    +    +    +    +    +    +    +
 0.35     +    +    +    +    +    +    +    +    +    +    +
 0.4      +    +    +    +    +    +    +    +    +    +    +
```

Rendered from `artifacts/phase_diagram.json`. The figure with the magnitudes and the
indifference band is `artifacts/figures/02_phase_diagram.png`; this map only shows the
sign, because a hand-transcribed grid is how the previous version of this section went
stale.

### What it says

| | `best_available` | `reference_sms` |
|---|---|---|
| Ahead in every pre-registered cell | yes | yes |
| Ahead, as a share of cells | **100%** | **100%** |
| Decisive (outside the indifference band) | **51%** | **51%** |
| Decisive at every self-heal level from | **not located by this grid** | **not located by this grid** |
| Median advantage per 1,000 cycles | ₹339,812 | ₹387,950 |
| Contact capacity binds up to | opt-out **0.1** | opt-out **0.05** |
| Capacity binds in | **16%** of cells | **12%** of cells |

**Three findings, in order of usefulness.**

1. **The boundary is not in this grid, and that is the result.** Antar is ahead in every
   cell of both panels, so the indifference boundary lies at or below the bottom edge of
   the pre-registered opt-out range (0.05) — outside the space we committed to. We
   cannot say where it is, and this section says so rather than quoting a number the
   grid does not contain. An earlier version of this card placed it at 0.035 on the
   strength of a post-hoc extension below the range; that extension is not in the
   committed artifact, and the claim went with it.

   A merchant still does not know which side they are on without measuring their own
   post-notification cancellation rate — and that measurement, not this simulator, is
   what would tell them.

2. **The scarce resource changes across the grid.** Contact capacity binds only at
   opt-out ≤ 0.1 in the multi-channel panel, which
   is 16% of cells; above that every capacity shadow price
   is **zero**, because Antar declines slots it is entitled to use. So the same merchant
   is running two different businesses depending on which side they sit: one where
   outbound capacity is the constraint, one where customer tolerance is. See
   `docs/LIMITATIONS.md` L14.

3. **Channel mix changes the magnitude a little, and the sign not at all.** An SMS-only
   merchant gains somewhat more from uplift allocation
   (₹387,950 vs
   ₹339,812 per 1,000, a
   14%
   difference), because a propensity ranker restricted to one channel has fewer ways to
   be accidentally right. The two panels are decisive in
   51% and 51% of cells
   respectively, so the channel mix does not change how *often* the advantage is
   decisive. An earlier version of this section claimed a much larger gap on both
   counts; it was reading a superseded run.

### What is wrong with it

- **Roughly half the cells are inside the indifference band.** Antar is ahead everywhere
  but decisively ahead in 51% of cells, on 250 customers per
  cell. "Ahead in every cell" is a weaker statement than it sounds, and the band is
  reported for that reason rather than hidden.
- **The committed run does not extend below the pre-registered range.** Locating the
  boundary needs `python -m scripts.make_phase_diagram --customers 250 --extend`, which
  adds rows below 0.05. Any claim about where the boundary sits requires that run and
  must be labelled post-hoc, because the extension is outside what §9.4 registered.
- **Every rupee figure here is expected value under the ground-truth response model**,
  not a realised outcome against the randomised control. That is the right choice for
  locating a boundary — sampling noise across 176 cells would blur it —
  and the wrong one for a headline. The inferential number comes from the holdout at M8.

Regenerate with `python -m scripts.make_phase_diagram --customers 250`.
Artifact: `artifacts/phase_diagram.json`.
