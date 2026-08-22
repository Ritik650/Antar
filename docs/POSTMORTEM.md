# POSTMORTEM.md — real defects, real fixes

The defect log kept during the build. No invented drama; every entry below is a bug
that existed in a commit, was caught by something, and was fixed.

PLAN.md section 13 says the strongest content here is the bug found in our *own
methodology*, not the library that gave us trouble. D1–D3 are that kind, and they are
uncomfortable enough to be worth reading in full.

| ID | Area | Found by | Severity |
|---|---|---|---|
| D1 | Simulator response model | Reading the code while a gate failed | **High** — invalidated the effect sizes |
| D2 | Simulator scenarios | Same investigation | **High** — an advertised axis was inert |
| D3 | Simulator response model | Same investigation | Medium — understated harm everywhere |
| D4 | Webhook fixtures | `test_every_consumed_fixture_parses` | Low |
| D5 | Circuit breaker | Test authoring | Low |
| D6 | Anti-circularity scan | A failing invariant test | **High** — inflated the headline finding |
| D7 | Simulator generator | `test_no_leakage` | Medium — destroyed a real signal |
| D8 | Changepoint detector | Detection report | **High** — 80% false-alarm rate |
| D9 | Failure emission | Detection report | **High** — a free 98% recall |
| D10 | Detection pipeline | Detection report | **High** — L2 read the answer key |
| D11 | Cost matrix | The ablation contradicted itself | Medium — priced labels, not actions |
| D12 | Changepoint detector | A failing unit test | **High** — segments stuck DEGRADED forever |

---

## D1 · The persuasion ceiling was not a ceiling

**Symptom.** `tests/statistical/test_anti_circularity.py` failed: the negative-uplift
population appeared in one scenario, not the required two.

**Investigation.** Rather than adjust the threshold — which would have been the
rationalisation `docs/EVALUATION.md` section 1.1 exists to forbid — we instrumented
the response model and printed the distribution of `P(persuaded)` under the best
channel for each customer:

```
mean_persuadability param : 0.28
P(persuaded) best-channel  : mean 0.461  p50 0.435  p90 0.875  max 0.920
share above the stated ceiling: 0.692
```

**Root cause.** `CustomerLatents.persuadability` is documented, in two places, as
"the ceiling on P(persuaded) under a perfectly chosen, perfectly timed contact". But
`ResponseModel.persuasion_probability` multiplied it by three modifiers that could
each exceed 1.0 — `channel_fit` up to 1.6, `tenure_bonus` up to ~1.4, `timing` up to
1.15 — so the effective ceiling was about 2.6× the stated one. 69% of customers were
above the value that was supposed to bound them, and the median dunning SMS was
persuading 43% of its recipients.

**Why it is a genuine defect and not a convenient adjustment.** Two independent
reasons, neither of which depends on the failing test. First, the code contradicted
its own docstring, which is a defect regardless of which way it moves any result.
Second, a 43% median response rate to a dunning message is not a credible number
under any parameterisation, and would have been indefensible in front of a payments
panel.

**Fix.** Every modifier confined to (0, 1], so `persuadability` is genuinely the
ceiling. Discounts are exempt and add on top, because a discount is an economic lever
rather than a messaging one — documented at the call site.

**Guard.** `tests/statistical/test_response_model_invariants.py` asserts the ceiling
holds for a large random population, so it cannot silently regress.

**Honest note.** This fix moves the result *toward* the finding we wanted. The
disclosure at the top of `test_anti_circularity.py` says so explicitly, along with
the before-and-after numbers.

---

## D2 · A scenario axis was cancelled by its own normaliser

**Symptom.** Found while investigating D1. The `conservative` scenario was producing
far less extra opt-out harm than its parameters implied.

**Root cause.**

```python
hazard = BASE_OPTOUT_HAZARD * latents.optout_sensitivity / scenario.mean_optout_sensitivity
```

`optout_sensitivity` is drawn from a Beta whose mean *is*
`scenario.mean_optout_sensitivity`. Dividing by it normalises the mean hazard to
`BASE_OPTOUT_HAZARD` in every scenario. `docs/SIMULATOR_CARD.md` section 8 advertises
mean opt-out sensitivity as one of four axes that distinguish the three
parameterisations, and in the code it moved only the dispersion, never the level.

A parameterisation that advertises an axis it does not use is worse than one that
does not advertise it, because a reader checking the card against the code would have
concluded the sensitivity analysis covered ground it did not cover.

**Fix.** A fixed `REFERENCE_OPTOUT_SENSITIVITY = 0.18` constant, not read from the
scenario. Base remains the reference regime by construction; conservative and
aggressive now differ in level as documented.

**Guard.** `test_the_optout_axis_actually_changes_the_hazard` asserts the mean hazard
is strictly ordered conservative > base > aggressive.

**Honest note.** This fix moves `aggressive` *away* from the finding — its
negative-uplift share fell to essentially zero. We report that as a genuine boundary
on the claim rather than a disappointment.

---

## D3 · Every event understated the opt-out hazard by one notification

**Root cause.** `prior_notifications` defaulted to `0`. But under RBI-EM-01 a
pre-transaction notification must precede *every* debit by 24 hours — so the cycle
that just failed had already delivered one cancel prompt to that customer. An
Antar-initiated retry is never the first notification of the month; it is at least
the second, and `NOTIFICATION_FATIGUE` applies to it.

The default was therefore wrong for every mandate-failure event in every batch, and
in the direction that flattered the treatment arm.

**Fix.** `BASELINE_NOTIFICATIONS = 1` is the default rather than a value each caller
has to remember to pass. A default that is correct for the common case beats a
comment asking people to be careful.

---

## D4 · Three consumed webhook types had no fixture

**Found by.** `test_every_consumed_fixture_parses`, which asserts that
`CONSUMED_EVENTS` and the fixture directory agree in both directions.

`payment.captured`, `subscription.resumed`, and `subscription.completed` were in the
consumed set with no fixture behind them — meaning three event types the system
claimed to handle had never been parsed even once.

**Fix.** Fixtures added. The value here is in the *shape* of the test: asserting only
that "every fixture parses" would have passed happily while the gap existed. Asserting
the set equality in both directions is what caught it.

---

## D5 · Circuit-breaker clock was a shared class attribute

**Found by.** Writing the half-open test, which needed to substitute a scripted clock.

`CircuitBreaker._monotonic` was a dataclass field defaulting to `time.monotonic`,
which makes it a class attribute shared by every breaker in the process. Substituting
it in one test would have affected every other breaker. It happened to work because
builtins are not descriptors, which is precisely the kind of accident that stops
working after an unrelated refactor.

**Fix.** Assigned per instance in `__post_init__`.

---

## D6 · The "balance peak" was the trough for a fifth of customers

**Symptom.** `test_timing_against_the_balance_curve_matters` failed: contacting
customer `cust_000007` at what the test called the balance peak persuaded *less* well
than contacting them at the trough.

**Root cause.** Both the test and — more seriously — the anti-circularity scan in
`antar/eval/claims.py` computed the peak as:

```python
peak = base.replace(day=min(latents.salary_day, 28)) + timedelta(days=1, hours=11)
```

The `min(..., 28)` clamp exists to avoid `ValueError` on short months. For a customer
paid on the 29th or 30th it silently moves the "peak" to the 28th, which under the
30-day wrap in `balance_fraction` is 29 days *after* payday: the emptiest their
account ever gets. Roughly a fifth of the population is paid on the 30th.

**Why it mattered more than a test bug.** The scan's whole claim is that it evaluates
uplift under the action *most favourable to treatment*, so that a customer who is
still negative is negative under anything Antar could have done. For the affected
fifth it was doing the opposite — evaluating the worst timing and calling it the best
— which inflated the negative-uplift population. The headline finding was measured
with the instrument miscalibrated in the direction that flattered it.

**Fix.** `CustomerLatents.next_balance_peak()` / `next_balance_trough()`, which
brute-force the search over the next 32 days instead of deriving a date. Month
lengths, the 30-day wrap, and the RBI-EM-01 24-hour floor interact in ways that are
tedious to reason about and cheap to search.

**Effect on the reported numbers.** Every scenario moved *down*: conservative
38.6% → 36.0%, base 5.3% → 5.1%, aggressive 0.0% → 0.07%. The gate still passes, and
base now clears its threshold by 0.13 percentage points rather than 0.3 — which is
why the README calls the base result marginal rather than positive.

---

## D7 · The merchant's record of tenure disagreed with the truth

**Found by.** `test_no_leakage`, which flagged `tenure_months` as a latent field
appearing on `CustomerContext`.

**First reading — a leak.** Wrong. Tenure is the one item in the latent vector a real
merchant genuinely observes: it is in their own subscription table. Withholding it
would model a merchant who cannot read their own database.

**Actual defect, found while confirming that.** The generator drew
`CustomerContext.tenure_months` from the `"customer"` substream and
`CustomerLatents.tenure_months` from the `"latents"` substream — two independent
draws of the same real-world quantity. So the merchant's record of tenure was
uncorrelated with the tenure that actually drove persuasion.

This is not a leak; it is arguably worse. A leak makes a model look better than it
is, and the leakage tests are looking for it. This made every uplift model look
*worse* than it should, by replacing a genuine signal with noise, and nothing in the
system would ever have reported it. It would have shown up, if at all, as a
disappointing Qini curve with no explanation.

**Fix.** `CustomerContext.tenure_months` is taken from the latents. `tenure_months`
is declared in a one-entry `OBSERVABLE_BY_DESIGN` allowlist with a written
justification, and two new tests guard it: one asserts the allowlist does not grow
without an ADR, the other asserts every observable field equals the latent it mirrors.

**Note on the allowlist.** An allowlist is the obvious way to defeat a leakage test,
so it is deliberately hostile to use: one entry, a length-checked justification
string, and a size assertion that fails on the second addition.

---

## D8 · The CUSUM compared unstandardised increments to standardised thresholds

`cusum_k` and the decision thresholds are documented as being in units of the
segment's baseline standard deviation. The accumulator was adding raw probability
deviations. With a 25% baseline failure rate, two consecutive failures crossed the
"degrading" line — and consecutive failures at a 25% rate are ordinary weather, not
an outage. **Measured false-alarm rate: 80%.**

**Fix.** Accumulate `z = (baseline - value) / sigma` so the increment and the
threshold share units. Guarded by `test_cusum_is_standardised`.

---

## D9 · Three error codes were emitted only by outages, handing the classifier a free 98% recall

`payment_timed_out`, `network_error`, and `server_error` appeared in `ISSUER_DOWN`'s
emission table and nowhere else. The taxonomy table marked them *ambiguous*, correctly
— but the data made them a perfect tell, and the classifier learned it. `ISSUER_DOWN`
recall came out at 0.98 on evidence that does not exist outside this simulator.

Real timeouts are not the exclusive property of an outage: a card the issuer's
tokenisation service cannot resolve times out exactly like a bank that is down.

**Fix.** `TECHNICAL_DECLINE` now emits all three. The recall it earns afterwards is a
recall it earned.

---

## D10 · The detection pipeline read the answer key

The worst of the batch. `build_detector` constructed the mandate FSM like this:

```python
lifecycle = [
    (event.subscription_id, "subscription.cancelled", event.occurred_at)
    for event in batch.events
    if batch.true_failure_class.get(event.event_id) is FailureClass.MANDATE_REVOKED
]
```

L2 was reading `true_failure_class` — the ground truth — to decide which mandates were
revoked, and then being graded on how well it identified revoked mandates. Reported
recall: **1.000.** Actual information used: the answer.

There was a second, subtler half. Even with an honest lifecycle stream, the analyser
called `state_of()` — the *current* state — while diagnosing a *historical* failure.
A cancellation webhook is timestamped at the moment of cancellation, which is the same
moment the debit failed, so `state_of()` let the detector see a cancellation it could
not have known about.

**Fix.** The generator emits an observable `lifecycle_events` stream, the FSM consumes
only that, and the analyser calls `state_at(subscription_id, when)`. `MANDATE_REVOKED`
recall fell from 1.000 to 0.960 and precision rose to 1.000, both now earned from the
error payload rather than from hindsight.

**Why the leakage tests did not catch it.** `test_no_leakage` checks that no production
layer *imports* the simulator, and `antar/detect/pipeline.py` does not — it receives the
batch as an argument and reads a ground-truth attribute off it. A static import check
cannot see that. The lesson is that a leak travels by data as easily as by import, and
the test that caught this one was a *plausibility* check on a suspiciously perfect
metric, not a structural check.

---

## D11 · The cost matrix priced labels instead of actions

The ablation study produced a contradiction: adding the downtime feed to a table-only
detector *increased* the total rupee cost. That made no sense, and the cost function
was the reason.

Cost was indexed by predicted *label*, so converting an `UNKNOWN` into a wrong
`ISSUER_DOWN` looked more expensive than leaving it unresolved — even though both
recommend `WAIT` and the merchant does exactly the same thing in each case. A detector
is not graded on the name it assigns. It is graded on what the name causes to happen.

**Fix.** `ACTION_COST[(InterventionClass, FailureClass)]` in `root_cause.py`, indexed by
the recommended action. Every cell carries a one-line rationale, and
`test_the_cost_matrix_covers_every_action_and_cause` asserts none is left unpriced.

---

## D12 · A lucky window poisoned a segment's baseline permanently

Symptom: `test_tracker_stays_healthy_through_ordinary_noise` failed with a **92%**
alarm rate at thresholds that a hand-calculation said should alarm about 0.1% of the
time.

Instrumenting the CUSUM trajectory showed it climbing monotonically to 148 and never
returning:

```
i= 40 state=HEALTHY   cusum=  0.000 base=0.936 ewma=0.967
i=100 state=DEGRADED  cusum= 39.901 base=0.936
i=599 state=DEGRADED  cusum=148.911 base=0.936
```

On returning to `HEALTHY` the tracker re-estimated its baseline as `self.ewma`. With
`alpha=0.2` the EWMA is effectively a five-observation average, and one lucky window
set the baseline to 0.967 on a rail whose true success rate was 0.75. Every subsequent
observation then looked like a deviation, the CUSUM drifted up forever, and the segment
was locked in `DEGRADED` for the remaining 560 observations.

**Fix.** Re-estimate from a 200-observation rolling window, Laplace-smoothed, and cap
the CUSUM at 3x the degraded threshold so no transient can lock a segment out.

**Effect.** False-alarm rate 92% → 0.5% *at the same thresholds*, with detection still
inside 8 observations. The thresholds had never been the problem, and the two rounds
of threshold-raising that preceded this discovery were treating a symptom. Worth
remembering: when a tuning parameter seems to need an implausible value, the parameter
is usually not the bug.

---

## What this log says about the build

Four of the six defects are in the simulator and the measurement apparatus, and all
four were found by statistical gates rather than by unit tests. That is the argument
for having the gates: none of D1–D3 or D6 would have produced a wrong *answer*
anywhere visible. They would have produced a *plausible* answer with the wrong effect
sizes, and nothing else in the system would have objected.

The uncomfortable part is the ordering. We went looking for defects because a test we
wanted to pass was failing, and we found several that moved the result — three toward
it, two away from it. Every one is independently defensible against the code's own
documented specification, and the before-and-after numbers are published next to the
test rather than buried. A reader who thinks that is still too convenient is entitled
to discount the sleeping-dogs magnitude accordingly — which is why
`docs/SIMULATOR_CARD.md` section 6.4 declines to defend the magnitude in the first
place, and why the base-scenario result is reported as marginal rather than positive.

The pattern worth naming: **D1, D2, and D6 were all cases where the code contradicted
its own docstring.** Not subtle logic errors — parameters that did not mean what they
said they meant. In a system whose entire claim is measurement discipline, that is the
failure mode to watch for, and prose that has drifted from the code it describes is
where it hides.
