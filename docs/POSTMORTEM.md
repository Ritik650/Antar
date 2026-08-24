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
| D13 | Claim scan | The gate failed overnight with no code change | **High** — a pre-registered threshold moved with the calendar |
| D14 | Exploration design | The first bake-off produced negative AUUC for everything | **High** — 74% of exploration rows were treated |
| D15 | Sign-recovery metric | Adding a trivial baseline | **High** — no learner beats a constant predictor on F1 |
| D16 | Retention measurement | A confidence interval of exactly (0, 0) | **High** — would have deleted two components on a measurement that could not see them |
| D17 | Phase-diagram reporting | Running a single panel | **High** — printed a fabricated cross-panel comparison |
| D18 | Phase-diagram panels | Two panels came out identical | **High** — the panel restriction never applied |

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

---

## D13 · The pre-registered gate moved across a midnight

**Symptom.** `test_negative_uplift_population_emerges_in_at_least_two_scenarios` failed
on a morning when the only changes since the previous green run were in the policy layer
— nothing that touches the simulator, the response model, or the scan.

The base scenario's negative-uplift share had gone from **5.13% to 4.67%**, crossing the
pre-registered 5% threshold and withdrawing the headline finding.

**Root cause.** `antar/eval/claims.py::best_available_uplift` evaluated the population at
`clock.now()`:

```python
base = clock.now()
peak = latents.next_balance_peak(base + timedelta(hours=24))
```

The scan places each customer's action at their next balance peak. Move the reference
instant by one day and the peak lands on a different day of the month for part of the
population, their `balance_fraction` changes, the persuasion term changes, and a handful
of customers cross zero. Nothing about the simulator changed. The calendar did.

**Why it is worse than it looks.** The number in question is the one a pre-registered
threshold is applied to. A gate that a date can flip is not a gate — and had this run at
a different hour it might have flipped the other way and *confirmed* the finding, which
is the same defect wearing a friendlier face.

It also violates the project's own clock discipline. `tests/unit/test_clock.py` forbids
`datetime.now()` outside `antar/clock.py`, and this code obeyed the letter of that rule
by calling `clock.now()` instead. The rule was written to stop *scheduling* logic drifting
with a skewed worker clock; it did not occur to me that a *measurement* would read the
clock at all.

**Fix.** `SCAN_REFERENCE`, a fixed instant inside the simulated horizon, and the same one
`tests/conftest.py` freezes to. `test_the_scan_does_not_depend_on_the_wall_clock` runs the
scan under three installed clocks a year and a half apart and requires an identical
answer.

**Effect on the reported numbers.** Pinned and averaged over five seeds: conservative
36.5%, base 5.83% (range 5.3–6.2%), aggressive 0.12%. The base margin is 0.8 points rather
than 0.13 — better, but still thin enough that the phase diagram in `docs/EVALUATION.md`
§9.4, not the threshold crossing, is the right way to report it.

**The general lesson.** A discipline rule that names a mechanism (`datetime.now()`) rather
than a property (determinism) leaves a gap exactly where someone obeys it literally. The
clock-discipline test now has a sibling that checks the property directly.


---

## D14 · The exploration split was 74% treated

**Symptom.** The first bake-off returned a **negative** AUUC for every learner, and
`random` scored the *best* Qini of the six candidates. A random scorer beating four
causal learners on a ranking metric is not a result, it is a broken instrument.

**Root cause.** Exploration drew uniformly from the feasible action set, and that set
was `{None, SMS, WhatsApp, Email, Voice}` — five options of which four are a contact.
So `P(treated) = 0.8`, and the measured split came out at **74% treated**.

Two things break at that balance. The untreated arm is too thin to fit an outcome model
on (142 rows in a single batch), so the T-learner's control model is noise. And the
Qini denominator `N_treated(k) / N_control(k)` degenerates when `N_control(k)` is near
zero at the top of the ranking, which is exactly where the curve is most informative.

**Fix.** Draw treatment first at 50/50, *then* a channel uniformly from the feasible
contacts. The propensity stays exactly known — `P(treated) = 0.5`,
`P(channel | treated) = 1/|feasible contacts|` — which is the property
`docs/EVALUATION.md` §7.1.1 depends on. Measured balance afterwards: **46.2% treated**.

**Effect.** AUUC went positive for every learner, `random` fell to a Qini of −0.108
(correctly ~0 for a non-informative scorer), and `propensity` fell to the *worst* Qini
of the set at −0.275 — which is the pedagogically important result `docs/EVALUATION.md`
§8 predicts, and which the broken instrument had been hiding.

**Note.** Uniform-over-actions is the natural reading of "a uniformly random
intervention" in `docs/SIMULATOR_CARD.md` §7, and it is what we implemented. It is
also a poor design for estimating a *contrast*. The card has been updated.

---

## D15 · No learner beats a constant predictor on sign recovery

**The most uncomfortable finding in the build, and it is reported rather than fixed.**

`docs/EVALUATION.md` §6.2 was amended before the bake-off to make negative-region sign
F1 a co-primary selection criterion, on the reasoning that AUUC can look excellent while
a model gets the sign wrong at the bottom of the ranking. That reasoning was right — the
amendment disqualified `causal_forest`, which had the second-best AUUC and a sign recall
of 0.006.

But the F1 numbers looked suspiciously flat, all clustered around 0.17–0.25 at a
prevalence of 0.15. So a trivial baseline was added: `always_abstain`, which declares
every customer negative.

| Model | sign F1 | abstention value |
|---|---|---|
| `always_abstain` (trivial) | **0.261** | **−₹43,668** |
| `r_learner` | 0.254 | +₹24,137 |
| `random` | 0.251 | −₹24,664 |
| `t_learner` | 0.242 | +₹9,701 |
| `x_learner` (selected) | 0.236 | +₹9,699 |
| `causal_forest` | 0.173 | +₹14,010 |

At prevalence *p*, a constant "everyone is negative" predictor scores precision *p*,
recall 1.0, and therefore **F1 = 2p/(1+p) = 0.261**. It beats every learner we fitted.

**What this means.** On the F1 metric, **the sign-recovery claim is not supported**. Our
learners do not identify the sleeping-dogs population better than a predictor that
identifies nothing.

**What it does not mean.** The rupee-denominated abstention value separates them
decisively, and in the right direction: `always_abstain` is the *worst* model by
₹43,668 because abstaining on everybody forgoes every positive uplift in the batch,
while `random` also loses money and the real learners all make it. A model with
precision 0.17 still makes money if it abstains on the customers where the harm is
*large* — F1 counts decisions, and money weighs them.

**The methodological finding.** F1 on a rare, thin-signal region is a poor selection
metric: a constant predictor sets a floor that a genuinely informative model can sit
below. The rupee metric is the one that discriminates, and it is the one PLAN.md asked
for in the first place.

**What we did not do.** We did **not** re-run selection with the rupee metric as the
co-primary. Doing so would have changed the winner from `x_learner` (+₹9,699) to
`r_learner` (+₹24,137), and swapping the selection criterion after seeing which model
it favours is precisely the behaviour `docs/EVALUATION.md` §1.1 exists to forbid. The
pre-registered rule selected `x_learner` and `x_learner` is what goes to the holdout.

The finding is logged as a candidate amendment for any future protocol, where it can be
committed **before** the numbers exist. That is the only order in which it would mean
anything.


---

## D16 · The retention measurement could not see the thing it was measuring

**Symptom.** The post-M6 ablation returned a confidence interval of **exactly (0, 0)**
for both governed components, and the pre-registered rule duly resolved both to DELETE.

A CI of exactly zero width is not a result. It is an instrument reading zero because it
is not plugged in.

**Root cause.** `PolicyRunner.candidate_values` passed `diagnosis=None` for every
event. The detection layer had **no influence whatsoever** on the allocation: candidate
values came from the ground-truth response model, and L2's `recommended_class` was
never consulted. Removing the downtime cross-check therefore could not change the
answer, because nothing downstream was reading it in the first place.

Deleting two working components on that basis would have been a false verdict wearing
the clothes of discipline — and it would have been *reported* as discipline, which is
worse.

**Fix.** L2's recommendation now gates Antar's candidate set: an `ISSUER_DOWN` diagnosis
recommends WAIT and a `MANDATE_REVOKED` one recommends TERMINATE, and both remove the
candidate before the LP sees it. That is the point at which the detection layer reaches
the money, and therefore the only place its retention verdict can be measured.

The gate applies to P3 alone, which is the honest representation of the three policies:
"contact everyone" does not diagnose, and a propensity ranker is a pure ML score with no
notion of root cause. Only P3 asks *why* the payment failed before deciding whether to
ask again.

**A second, smaller version of the same error.** Disabling the downtime cross-check was
implemented as `downtime_overlap_tolerance_minutes: -1`. A window that strictly covers
the attempt is still found at a negative tolerance, so the component stayed live and the
"without" arm was not actually without it. Now `-100000`.

**Effect on the verdict.** Re-measured across 3 seeds at 800 customers with L2 actually
connected:

| Component | Δ net per 1,000 | 95% CI | Verdict |
|---|---|---|---|
| `downtime_crosscheck` | ₹0.00 | [0, 0] | DELETE |
| `changepoint_detector` | **−₹846.76** | [−2,540, 0] | DELETE |

Both still DELETE — but now on a measurement that could have said otherwise, and with
the changepoint detector shown to be *actively costing money* rather than merely not
earning its keep.

**The general lesson.** A null result from an ablation should be treated as suspicious
until you have confirmed the component was connected. "No effect" and "no measurement"
produce identical numbers.

---

## D17 · The phase diagram printed a comparison it had not computed

**Symptom.** A single-panel smoke run printed:

> Antar beats propensity targeting in 100% of the parameter space for a merchant with
> every channel, and **0% for one with only SMS**.

The SMS panel had not been run. Not one cell of it existed.

**Root cause.** `_interpretation` read `win_share("reference_sms")`, which computes
`wins / len(cells)` and returns `0.0` for an empty list. `cells == 0` and `wins == 0`
produced the same number, and the sentence generator could not tell them apart.

**Why it matters more than a display bug.** This is a *generated* sentence intended for
the README and the pitch video — exactly the class of artifact PLAN.md rule 6 exists to
protect ("never fabricate a number"). The machinery for generating prose from
measurements is the machinery for generating confident prose from no measurement at all,
and it did so on its first outing.

**Fix.** `_interpretation` distinguishes "not computed" from "computed as zero" and says
so explicitly: *"No cross-panel comparison is available — reference_sms was not run."*

**The general lesson.** Any function that turns numbers into sentences needs an explicit
branch for "there is no number", and that branch has to be exercised. A default of zero
is a lie with a plausible face.


---

## D18 · The second phase-diagram panel was a copy of the first

**Symptom.** Both panels finished. Their medians differed by **₹7 out of ₹1.9 million**,
and the generated interpretation read:

> The boundary is stable across panels (100% vs 100% of the grid). The merchant's
> channel mix is not the deciding factor.

Two panels agreeing to seven significant figures is not a finding about channel mix. It
is one panel run twice.

**Root cause.** `reference_sms` was implemented by overriding the *cost table*:

```python
cell_config = config.with_overrides(
    {"simulator.costs.channel_paise": {"SMS": 25, "SILENT_RETRY": 0}}
)
```

But the action set came from `EXPLORATION_CHANNELS`, a **module constant**, and cost
lookups used `.get(channel, 0)`. So WhatsApp, email and voice remained fully available
and became *free*. The panel intended to model a merchant with one integration modelled
a merchant with four integrations and no marketing budget.

**Fix.** `simulator.available_channels` in config, read by
`ExperimentRunner`. The restriction now applies to the action set, which is the thing
that needed restricting.

**Why this one stings.** It is the same failure as D17, twenty minutes later: a
generated sentence stating a comparison with total confidence when the underlying
computation had not happened. D17 was "the panel was never run"; D18 is "the panel ran
but was not the panel we said it was". Both would have gone into the README as findings.

**The general lesson, now stated twice.** Configuration that *looks* like it restricts
behaviour must be checked to actually restrict it. A cheap assertion — that the two
panels differ at all — would have caught this immediately, and one now exists in
`tests/unit/test_phase_diagram.py`.

---

## D19 · The contamination rule matched only the digits

**Found by:** the adversarial suite, on its first run against the finished detector.
**Severity:** high — an unlawful send would have gone out.
**Status:** fixed.

**Symptom.** `tests/adversarial/test_contamination.py` includes a case written as prose
rather than as a template:

> "Your payment failed. We also have twenty percent off annual plans."

It was not blocked. `DISCOUNT_OFFER`, the rule whose entire job is to catch a percentage
discount, scored it 0.0 and the message passed as TRANSACTIONAL.

**Root cause.** The pattern was

```python
r"\b\d{1,3}\s?(%|per\s?cent|percent)\s*(off|discount|cashback|back)\b"
```

`\d{1,3}` matches `20`. It does not match `twenty`. Every example I had written while
building the rule — every one — used digits, because I was thinking about the *rendered
template*, where an amount arrives as a formatted number. But the contamination detector
does not inspect templates. It inspects **LLM prose**, and a model writing a sentence
writes "twenty percent" far more often than "20 percent". The rule was checking the case
least likely to occur in the only input it ever sees.

**Fix.** A `NUMBER_WORD` alternation folded into a shared `QUANTITY` group used by both
`DISCOUNT_OFFER` and `MONEY_OFF`, plus a new `OFF_A_PLAN` rule that matches
`off our|your|the|annual|premium…` with no quantity at all. The second rule is the
important half: it catches the construction when the quantity is phrased in a way no
quantity pattern anticipated, which is the failure mode a quantity pattern always
eventually has. Both rules now fire on the original case, and the test asserts
`"DISCOUNT_OFFER" in triggered_rules` rather than equality — overlapping coverage is the
design, not a redundancy to trim.

**Why this one matters more than its size.** Every other defect in this log was caught by
a test I wrote against my own implementation. This one was caught by a test written
against the *specification* — PLAN.md §9.4 asks for "dunning message with an upsell
appended", and I wrote the upsell the way a language model would, not the way my regex
would. The gap between those two sentences is the whole argument for adversarial testing
as a separate discipline: a suite written by the same mind that wrote the code, in the
same session, still tests the author's assumptions unless it is deliberately written
from the attacker's side.

**A second finding, and a near-miss of my own.** My first draft of this entry said the
classifier had scored the sentence 0.31 — "below threshold, but moving in the right
direction" — as evidence that the hybrid design was sound and only one half had a bug.
I had not measured it. When I did, the classifier scored it **0.0**. `LexicalClassifier`
keys on promotional-register vocabulary, and "twenty percent off annual plans" contains
none of its terms: not `discount`, not `offer`, not `deal`. **Both halves of the hybrid
missed this sentence completely.**

That is a materially different finding from the one I nearly wrote down. The hybrid's
premise is that the classifier catches phrasings nobody wrote a rule for; here it caught
nothing, because a lexicon is a keyword list wearing a different hat and inherits exactly
the same blind spot as the rule it was supposed to back up. Recorded as **LIMITATIONS
L15** rather than papered over: the classifier half of the detector is load-bearing in
principle and thin in practice, and the deterministic rules are doing essentially all of
the work.

I am leaving the sentence about the 0.31 in this entry rather than deleting it, because
it is the most instructive thing in the log. Rule 6 of this project is *never fabricate a
number*, and I produced a plausible one — with a confident interpretation attached —
inside the postmortem entry about a defect caught by not trusting my own assumptions.
It survived about ninety seconds, because the number was checkable and I checked it. The
control worked. The instinct that generated the number is still there.

**Generalisation applied.** A grep for other digit-only patterns across the rule set
found none, but the shared `QUANTITY` constant now exists so that the next
quantity-matching rule inherits word-numbers by construction rather than by remembering.

---

## D20 · The template registry did not cover the actions the allocator could choose

**Found by:** the M8 end-to-end pipeline, on its first complete run.
**Severity:** high — a crash in the send path, then a *false statement* in a message.
**Status:** fixed, in two stages, and the second stage is the interesting one.

**Stage one — the crash.** The pipeline died on
`TemplateError: no registered template for channel VOICE`. `CONTACT_CHANNELS` — the set
L3 selects from — contains `SMS`, `WHATSAPP`, `VOICE`, `EMAIL`. `registry.yaml`
contained templates for three of them. Nothing compared the two sets, so L3 could choose
an action L4 was structurally unable to perform.

Every earlier test of the act layer passed a channel *it had chosen itself*. The
adversarial suite, the drafter tests, the contamination tests — all of them constructed
an `Intervention` with `Channel.SMS` because that is the channel a person writing a test
reaches for. The gap needed the decide layer to pick the channel, and until M8 the two
layers had never actually been connected.

**Stage two — resolving is not the same as being right.** I added
`RETRY_SCHEDULED_VOICE`, added `test_every_contact_channel_has_a_template`, and the
suite went green. Then the first live trace showed this, for an event L2 had classified
`AFA_REQUIRED` at 97% confidence:

> "This is a service call from Antar. Your payment of Rs 20,313 ... could not be
> completed. **We will try again on 2 Apr 2026.**"

No, we will not. An AFA-required charge cannot succeed unattended — that is the entire
content of the classification. The message stated, to a customer, something the system
knew to be false, and it did so because `choose_template` fell back to the default when
`TEMPLATE_FOR` had no entry for `(VOICE, AFA_REQUIRED)`.

**The lesson, which is bigger than the bug.** My completeness test asserted that a
lookup *returned something*. It could not have caught this, because it never asked
whether the something was true. Test coverage of a mapping is not coverage of the
mapping's meaning.

**Fix.** Four more templates (`AFA_AUTHENTICATION_VOICE`, `INSTRUMENT_UPDATE_VOICE`, and
the two WhatsApp equivalents), so that every channel able to carry `AFA_REQUIRED` or
`TECHNICAL_DECLINE` has a truthful script. `MANDATE_REVOKED` on a push channel now
**raises**: a revoked mandate cannot be retried or re-debited, the customer would have to
authorise a new one, and there is no truthful short message for that — so the code
refuses rather than composing a plausible sentence. And
`test_a_retry_is_never_promised_where_a_retry_cannot_work` asserts the semantic property
across the whole cross product, with a paired test that some template *does* promise a
retry, so the invariant cannot pass by there being no retry language anywhere.

---

## D21 · `x or Default()` threw away every caller's ledger

**Found by:** a trace that came back empty on a run whose summary said 13 contacts.
**Severity:** high — the audit record silently did not exist.
**Status:** fixed, with a scan.

**Symptom.** `run_pipeline` reported `contacted: 13` and `ledger_entries: 1600`.
400 events x 4 entries = 1600 exactly. Not one `ACTION` entry had been written, and
`build_trace` found no action for any event.

**Root cause.** One line in `PolicyGate.__init__`:

```python
self.ledger = ledger or NullLedger()
```

`Ledger` defines `__len__`. An **empty** ledger is therefore falsy, so a caller passing
a fresh ledger got a `NullLedger` — and every gate decision went into a sink and
vanished. The bug is invisible at the call site, invisible in the type signature, and
appears only when the collection is empty, which for an append-only ledger is the
**first run, every time**.

**Fix.** `NullLedger() if ledger is None else ledger`, and the same correction in three
other places the same idiom had reached: `antar/pipeline.py`, `signals/downtime.py`
(`DowntimeRegistry`), and `signals/webhook_receiver.py` (`InMemoryEventStore`) — all
three classes define `__len__`, and all three would have discarded an empty object a
caller deliberately passed.

**The guard.** `tests/unit/test_default_substitution.py` walks the package AST for
`name = name or Call()` and fails if the callee's class defines `__len__` or `__bool__`.
It discovers its own targets rather than listing the ones I remembered, and a companion
test asserts the detector still recognises `Ledger` — a guard nobody has watched fail is
a guard nobody knows works.

---

## D22 · The pipeline recorded the model's estimate and acted on the oracle's

**Found by:** reading a trace I had just written, and not believing it.
**Severity:** critical — the ledger would have attributed simulator knowledge to the model.
**Status:** fixed.

**Symptom.** A trace read:

> "L3 estimated an uplift of +0.3072 (95% CI +0.2572 to +0.3572) and chose VOICE
> scheduled for 2026-04-02T19:20:00+05:30."

Both halves were true. The connective was false. The estimate came from the fitted
X-learner; the *choice* came from `PolicyRunner.candidate_values()`, which values every
candidate from `ResponseModel` — the simulator's ground truth — and picks the best
channel by that value.

**Why that is worse than a wrong number.** `eval/policies.py` is *right* to use ground
truth: comparing three policies fairly means valuing all three on the same oracle. Reused
in the production path it becomes a leak with a decision's clothes on. The ledger — the
artifact whose entire purpose is to be checkable — would have been recording the
simulator's answer key as L3's reasoning, hash-chained and tamper-evident and wrong.

**Fix.** The pipeline no longer calls `candidate_values`. `_build_candidates` picks the
cheapest feasible contact; `_estimate_values` values it from two fitted models and
nothing else:

```
net = recovery_uplift x amount - channel cost - optout_uplift x amount x multiplier
```

Ground truth now enters at exactly one point, `_realise`, which draws the simulated
outcome — the simulator's job, labelled `"simulated": true` in every artifact (N6).

**Three things fell out of the fix.**

1. **The harm model had to be built.** Valuing a contact without an opt-out term treats
   every contact as free of harm, which is the exact failure this project argues
   against. `OptoutRisk` now estimates it.
2. **A T-learner cannot estimate opt-out on this data.** Base scenario, seed 7: **0
   opt-outs in 2,996 untreated rows, 35 in 260 treated.** The control arm has one class.
   That is a property of the simulator, which models opt-out purely as a
   contact-triggered hazard — real customers cancel for reasons no merchant caused.
   `OptoutRisk` fits `P(optout | X, treated)` and subtracts the *measured* untreated rate
   rather than assuming it is zero. LIMITATIONS L17.
3. **The numbers moved, in the direction that makes sense.** Contacts on a 400-event
   slice went 12 → 29 and opt-outs 0 → 5. A model-driven policy contacts more than an
   oracle-driven one and induces harm the oracle could dodge. The oracle policy looked
   better because it was cheating.

**What this says about the earlier milestones.** M5 and M6 are unaffected — they
*are* the oracle comparison, and are labelled as such. What was wrong was carrying that
valuation into the path that writes the audit record. The two now share the feasibility
logic and nothing else.

---

## D23 · An LLM timeout crashed the send path the fallback existed to protect

**Found by:** the chaos suite, writing the row PLAN.md section 10 specifies as
*"LLM timeout → deterministic template fallback, flagged in the trace"*.
**Severity:** high — an unhandled exception in the money path.
**Status:** fixed.

**Symptom.** A client whose `messages.create` raises `TimeoutError` did not produce a
fallback draft. It produced a `TimeoutError`, out of `Drafter.draft()`, up through the
pipeline, killing the batch.

**Root cause.** One `except` clause:

```python
except (TemplateError, ValueError, json.JSONDecodeError) as exc:
```

Those are the three ways the model's **response** can be wrong: an unrenderable
template, an invented slot, malformed JSON. They are not the ways the **call** can be
wrong — a read timeout, a dropped connection, a 529, an SDK that reorganises its
exception hierarchy in a minor release. I had written a careful, specific exception
tuple and it was specific about the wrong axis.

The whole architecture of the act layer says the deterministic template is always safe
to fall back to. Given that, there is no exception from an API call worth crashing for,
and the narrow clause was precision doing damage.

**Fix.** `except Exception`, with a comment saying why the breadth is deliberate rather
than lazy. Every failure is appended to `drafter.failures` and reaches the ledger.

**A second defect in the same function, found by the next test.** On exhausting its
attempts, `_draft_with_model` returned `None` and let `draft()` build the fallback with
`repair_attempts=0`. A draft that had cost two API calls was recorded as never having
tried one. PLAN.md asks for the repair and the fallback to be **both logged**; only the
fallback was. `_draft_with_model` now builds the fallback itself so the count survives,
and `repair_attempts` means repairs — calls after the first — on both the success path
and the exhaustion path, which it previously did not.

**Why the adversarial suite missed both.** `tests/adversarial/test_injection.py` has a
test called `test_malformed_json_is_repaired_once_then_falls_back`. It passed throughout.
It asserted that the fallback was *used* — `draft.fallback_used is True` — and never
looked at what the record said about how it got there. The adversarial suite asks "can
an attacker make this do the wrong thing?"; the chaos suite asks "when this breaks, is
what we wrote down still true?". Those are different questions, and the second one found
two bugs the first had been stepping over for a day.

---

## D24 · A headline number in an artifact was a different quantity entirely

**Found by:** rendering `RESULTS.md` from the artifacts for the first time and reading
a sentence that contradicted the table directly above it.
**Severity:** critical — a published headline, wrong, with the wrong sign, for two
milestones.
**Status:** fixed, with a guard.

**Symptom.** `artifacts/RESULTS.md` rendered:

| Policy | ... | Net per 1,000 Rs |
|---|---|---|
| propensity | ... | -1,086,143 |
| antar | ... | 40,366 |

immediately followed by:

> **Antar minus propensity targeting: Rs -84,676.49 per 1,000 at-risk cycles.**

Antar is over a million rupees ahead per 1,000 cycles. The headline said it was eighty
thousand behind.

**Root cause.** `scripts/run_allocation.py`:

```python
delta = antar_minus_propensity_paise(outcomes) / 100     # line 139: correct
...
for component in (...):
    delta, ci, ... = retention_across_seeds(...)          # line 171: reused the name
...
"antar_minus_propensity_per_1000_rupees": round(delta, 2)  # line 198: last component's
```

The value written under the headline key was **the changepoint detector's retention
effect, in paise, under a key whose name ends `_rupees`** — the wrong quantity *and* the
wrong unit, from a loop thirty lines below. Compare the same run's retention block:
`changepoint_detector, delta_net_paise: -84676`. The number printed to the console at
line 140 was right; the number saved to the artifact was not. Nobody reads the console
after the run; everybody reads the artifact.

**Why it survived two milestones.** Every safeguard this project has was pointed
somewhere else. `-84,676.49` is plausible in magnitude, correct in units, correctly
rounded, and sits under a correctly-named key in a correctly-shaped JSON file. Nothing
compared it to the components it claimed to be derived from — because until M8's
`RESULTS.md` there was no consumer of the artifact that put the delta and the policy
table on the same page.

**Fix.** `headline_delta_rupees` and `retention_delta`, two names for two quantities.

**The guard, which is the point.**
`tests/statistical/test_artifacts_are_self_consistent.py` asserts that every derived
number in an artifact is recomputable from that artifact's own components: the delta
equals the difference of the two nets, each policy's net equals its parts, contacts plus
abstentions equals events, per-class support sums to the events evaluated, the ledger
entry count reconciles with the batch summary. Written against the stale artifact it
failed immediately with the arithmetic spelled out — which is how it should have been
found.

**The general lesson.** I have spent this build guarding the *inputs* to numbers: no
leakage, no answer key, pinned clocks, pre-registered rules, verified regulations. This
was a failure at the *output* — an artifact that was internally inconsistent, which no
amount of upstream care can catch. Any number that is a function of other numbers in
the same file should be checked against them, and the check is cheap.

**Scope of the correction.** No claim outside `artifacts/` used the bad figure: the
README does not exist yet, and the milestone commit messages quote the console output,
which was correct. The corrected headline for the base scenario is
**Rs 1,033,289 per 1,000 at-risk cycles**, and it is now checked by the guard on every
CI run. (That figure was itself corrected again in D27, which found the denominator was
candidates rather than at-risk cycles - the same field, a third time.)

---

## D25 · The retention rule stopped being able to measure anything, by succeeding

**Found by:** re-running `scripts/run_allocation.py` in M8 and noticing that the
retention verdicts had become `delta 0, CI (0, 0)` for **both** components, where M6 had
measured `-846.76` with a CI of `[-2540, 0]` for the changepoint detector.
**Severity:** high — a pre-registered measurement silently became incapable of producing
a result, while continuing to produce one.
**Status:** fixed, with a vacuity guard.

**Symptom.** Identical zeros, for both components, across every seed. Not "small". Not
"noisy". Exactly zero, every time — and the diagnostic line the script had been printing
all along said so, if anyone had read it:

```
downtime_crosscheck    DELETE
  with Rs 29,192/1000  without Rs 29,192/1000  delta Rs 0.00
```

The two arms are the same number to the rupee. That is not a small effect; that is not
an effect.

**Root cause, which is almost funny.** `retention_across_seeds` built the *with-component*
arm from the current config and the *without* arm by overriding some thresholds:

```python
with_component = PolicyRunner(batch, log, config=config, ...)
without        = PolicyRunner(batch, log, config=stripped, ...)
```

In M6 that was correct: both components were enabled in `config/default.yaml`, so the two
arms genuinely differed. **Then M6's own verdict switched them off.** From that commit
onward `config` already had `enable_downtime_crosscheck: false`, the stripped config
disabled a component that was not running, and the two arms described the same detector.

The measurement did not fail. It reported a delta of zero with a confidence interval of
zero width, and the pre-registered rule read that as ambiguity and resolved — correctly,
by its own terms — to **DELETE**. A component deleted once could never be reconsidered on
new data, because every future measurement of it would return exactly zero and confirm
the deletion. The rule had become a ratchet.

**The uncomfortable part.** The *verdict* did not change: both components were deleted in
M6 on a valid measurement, and both remain deleted. Nothing shipped is wrong because of
this. But between M6 and now, the artifact reported a zero delta as if it were a
measurement, `RESULTS.md` rendered it, and I had already overwritten M6's real numbers on
disk before noticing. The reason I noticed at all is that the README quoted M6's figures
and `test_results_are_reproducible.py` failed because they no longer appeared in any
artifact — a test written for a completely different purpose.

**Fix.** Both arms are now constructed explicitly, and neither inherits the shipped
default:

```python
enabled  = config.with_overrides({flag: True})
stripped = enabled.with_overrides({flag: False, **extra_off})
```

The measurement is now independent of what the current build happens to ship, which is
the only way a deleted component can ever be re-evaluated.

**The guard.** `retention_across_seeds` raises if the enabled and stripped arms produce
identical results on every seed:

> the retention measurement for 'changepoint_detector' is vacuous: the enabled and
> stripped arms produced identical results on every seed, so the component had no
> influence to measure. […] A zero delta from this state is not evidence for DELETE —
> it is the absence of evidence.

**The general lesson, and it is the third time this build has taught it.** D16 was a
retention CI of exactly (0, 0) because L2 had no influence on the allocator. D24 was a
headline that did not match its own components. This is the same shape again: **a
measurement that cannot fail is not a measurement, and "exactly zero" is the signature.**
Any estimator that can return a degenerate answer needs an explicit check that it did not,
because the degenerate answer is always plausible and always wrong.

---

## D26 · The trace assembler was quadratic, and the full batch looked like a hang

**Found by:** running `scripts/run_batch.py` at full size for the first time. Every run
until then had used `--max-events` or a reduced customer count.
**Severity:** medium — no wrong answers, but the demo path did not finish.
**Status:** fixed.

**Symptom.** `python -m scripts.run_batch --scenario base` printed its first line and
then produced nothing for eight minutes. Not an error, not a partial result. Silence.

**Root cause.** `build_trace(ledger, event_id)` does two full passes over the ledger: it
reads every entry, and it verifies the entire chain. That is correct for *one* trace and
it is what makes a single trace trustworthy — the verification covers the whole chain,
because a break anywhere is a reason to distrust a trace from anywhere.

`replay_is_self_consistent` then called it **once per event**. On a full base batch that
is 3,436 events against roughly 14,000 entries: about 48 million row constructions and
3,436 complete chain verifications, to answer a question that needs one pass.

**Why it survived every test.** `tests/unit/test_trace.py` uses a five-entry ledger.
`tests/integration/test_full_batch.py` caps at 150 events. Both are the right size for
what they test — composition, not scale — and neither could have surfaced this. The
performance characteristic only appears at the size the *demo* runs at, and until M8
nothing had run at that size.

**Fix.** `TraceIndex` loads the ledger once, verifies once, and groups entries by event
id in a single pass, resolving entries that carry only a `decision_id` or `action_id`
through whichever event claimed that id earlier. `replay.py`, the console, and the batch
runner all use it. `build_trace` is unchanged and remains the single-trace path, because
its whole-ledger verification is the honest thing to do when you are looking at one
event.

**The test that makes the optimisation safe.**
`test_the_index_and_the_single_trace_agree` builds every trace both ways on the same
ledger and asserts the dictionaries are identical. A faster path that returns different
answers is not an optimisation, and two code paths that are supposed to agree will not
stay agreed on their own.

**The general lesson.** Every test in this repository is small on purpose — small tests
are fast, and fast tests get run. The cost is that a whole class of defect, the kind that
only appears at production scale, cannot be caught by any of them. The batch runner is
now the thing that runs at full size, and it should be run at full size before every
demo, because it is the only place this class of bug can surface.

---

## D27 · The primary metric divided by the wrong denominator

**Found by:** a dimensionless sanity check run on the headline before it went into the
video — recovery as a fraction of the money at risk.
**Severity:** medium — the headline was overstated by 9%, and mislabelled in a way that
would not have survived a panel question.
**Status:** fixed.

**The check that found it.** `+₹1,126,509 per 1,000 cycles` is ₹1,126 per at-risk cycle.
Against a mean cycle value of ₹9,274 that is 12%, which is not absurd — but the field it
came from had just spent two milestones holding paise mislabelled as rupees (D24), so
"not absurd" was not good enough. A ratio of two quantities in the same units cannot have
a units error, so:

| Policy | Incremental recovery ÷ money at risk |
|---|---:|
| contact_everyone | 1.07% |
| propensity | 1.05% |
| **antar** | **1.12%** |

Sane, and no units error. But computing it required the denominator, and the denominator
did not match.

**Root cause.** `PolicyOutcome.events` was set to `len(candidates)` — events for which a
*feasible contact existed*. `net_per_1000_paise` divided by it. `docs/EVALUATION.md`
§11.2 pre-registered the primary metric as *"incremental rupees recovered per 1,000
**at-risk cycles**"*, and the README said so too.

On the base scenario: **3,436 at-risk events, 3,148 candidates.** The 288-event gap is
the events where every candidate contact was removed by a blocking regulation before the
solver saw it — which is exactly the population the pre-registered denominator was
meant to include, because a cycle Antar was forbidden to touch is still a cycle at risk.

The headline was therefore overstated by 3436/3148 = **9.2%**.

**Fix.** `PolicyOutcome` carries `candidates` and `at_risk_events` as separate fields.
The per-1,000 metrics divide by `at_risk_events`. The corrected headline is
**₹1,033,289 per 1,000 at-risk cycles**, and every downstream artifact, figure and
document now uses it.

**The second thing the check found, which matters more than the first.** Decomposing the
headline (per 1,000 at-risk cycles):

| Component of the ₹1,033,289 | Share |
|---|---:|
| Difference in expected recovery | ₹6,322 — **0.6%** |
| Difference in avoided opt-out loss | ₹1,026,962 — **99.4%** |

**The headline is not a recovery number.** It is almost entirely a harm-avoidance number.
Antar does recover marginally more than the propensity ranker (₹356,231 against
₹334,534), but that difference is a rounding error next to the opt-out loss it declines
to incur. The README said *"Antar recovers more money than the propensity ranker while
contacting a fifth as many people"* — true, and a sentence that lets a reader believe the
million rupees came from recovery. Corrected.

**And the third.** That harm term is `p_optout × amount × optout_loss_multiplier`, and
`optout_loss_multiplier` is **6.0** — a config parameter asserting that an induced
cancellation costs six cycles. It is an assumption, not a measurement, and it scales
99.4% of the headline linearly. Recorded as **LIMITATIONS L18**.

**The general lesson.** Both output-side guards built after D24 check *where a number came
from*: provenance (does it appear in an artifact?) and internal consistency (does it equal
its own components?). Neither asks *whether it is a plausible size*. A units error
produces a number that is perfectly traceable, perfectly consistent, and off by a factor
of a hundred. `tests/statistical/test_magnitudes_are_plausible.py` is the third guard, and
it works by reducing every headline to a dimensionless ratio — because a ratio cannot have
a units error at all.
