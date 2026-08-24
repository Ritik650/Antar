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
