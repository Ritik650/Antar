# EVALUATION.md — Pre-registered protocol

**Status: PRE-REGISTRATION. Commit this file before running the model bake-off (M5) or the final
evaluation (M8).**

This document specifies what will be measured, how, and what result would count as a failure —
written before the numbers exist. Its value is entirely in the ordering. A pre-registration
written after seeing results is not a pre-registration, it is a rationalisation.

Git history is the proof. Do not amend, rebase, or force-push the commit that introduces this
file.

---

## 1. Pre-registration discipline

### 1.1 The rules

1. This file is committed **before** `antar/decide/uplift/bakeoff.py` is run for the first time.
2. Section 6 (model selection) is fixed before any test-set performance is observed.
3. Section 4 (primary metric) is fixed before any holdout is unblinded.
4. Any deviation from this protocol is recorded in Section 13 with a reason and a timestamp.
   Deviations are permitted. Undocumented deviations are not.
5. The control holdout is unblinded **exactly once**, at M8. No interim peeking. Development
   uses the exploration split (§3.4), never the control.

### 1.2 Why this is in the repo at all

Two reasons, one honest and one strategic.

The honest one: uplift modelling is unusually easy to fool yourself with. Small effects, high
variance, many defensible modelling choices, and a metric that improves when you pick the
flattering split. Pre-registration is the cheapest available defence.

The strategic one: a panel evaluating "does this person understand measurement" will find this
file, check the git timestamp, and learn more from that than from any number in the README.

---

## 2. Hypotheses

Stated so they can fail.

| ID | Hypothesis | Falsified if |
|---|---|---|
| **H1** | Gross recovery materially overstates incremental recovery. | The gap between gross and incremental rupees is not statistically distinguishable from zero. |
| **H2** | Uplift-based targeting beats propensity-based targeting on incremental rupees per contact. | Propensity targeting matches or beats uplift targeting, CIs overlapping, in the base scenario. |
| **H3** | A non-trivial population has negative uplift — contacting them reduces net recovery, driven by the RBI-mandated notification opt-out. | No negative-uplift mass emerges, or it emerges only under scenario-specific tuning (see the anti-circularity test in the simulator card). |
| **H4** | Regulatory constraints are binding, i.e. compliance has a measurable rupee cost that can be quantified via LP duals. | All constraints are slack at the optimum; the shadow prices are zero and the constraint layer is decorative. |

**If H2 or H3 is falsified, we report that.** A negative result honestly reported is a stronger
submission than a positive result that does not survive scrutiny. The README must contain the
outcome of every hypothesis above, including the ones that failed.

---

## 3. Experimental design

### 3.1 Unit of randomisation

The **customer**, not the event. Randomising at the event level would leak treatment across
cycles for the same person and contaminate the control. Assignment is sticky for the whole
120-day horizon.

### 3.2 Arms

| Arm | Share | Description |
|---|---|---|
| `CONTROL` | 20% | No intervention of any kind. Pre-debit notifications still fire where legally required, because they are the issuer's obligation, not our action. This is the counterfactual baseline. |
| `TREATMENT` | 80% | Subject to the policy under test. Of these, ε = 20% receive a uniformly random feasible action (exploration), with propensity logged. |

**Note on the control arm and notifications.** Because `RBI-EM-01` mandates a pre-transaction
notification regardless of our involvement, control customers still receive them for scheduled
debits. What control customers do *not* receive is any *additional* Antar-initiated retry,
message, or discount. This is the correct counterfactual: it isolates our marginal contribution
rather than the effect of the regulatory baseline. State this explicitly in the README —
a careful reviewer will ask.

### 3.3 Stratification

Assignment is stratified by:
- issuer × method segment (12 strata)
- amount bucket, with a cut at the ₹15,000 AFA threshold
- merchant category (drives the `RBI-EM-04` threshold)

Balance check: standardised mean difference on every feature between arms, reported as a
covariate balance table. Any SMD above 0.10 is investigated before results are believed.

### 3.4 Splits

| Split | Purpose | May be used for |
|---|---|---|
| Exploration split (from ε-randomised treatment events) | Model development | Training, validation, hyperparameters, all iteration |
| Control holdout | Primary metric | Unblinded once, at M8 |

`tests/statistical/test_control_isolation.py` fails the build if any control `event_id` appears
in any training index. This is a hard gate, not a guideline.

### 3.5 Randomisation mechanism

Deterministic hash of `(customer_id, experiment_salt)`, so assignment is reproducible from the
seed and cannot drift between runs. The salt is committed.

---

## 4. Primary metric

**Incremental rupees recovered per 1,000 at-risk cycles.**

$$
\widehat{\Delta} = \frac{1000}{N}\left( \frac{\sum_{i \in T} R_i}{|T|} \cdot N - \frac{\sum_{i \in C} R_i}{|C|} \cdot N \right)
$$

where $R_i$ is rupees recovered attributable to cycle $i$ within the measurement window, $T$ is
the treatment arm, $C$ the control arm, and $N$ the number of at-risk cycles.

**Reported with a 95% bootstrap confidence interval**, 10,000 resamples, clustered at the
customer level (because assignment is at the customer level, resampling at the event level would
understate variance).

### 4.1 Measurement window

Recovery is attributed if the debit succeeds within **30 simulated days** of the initial failure.
Anything later is counted as not recovered. The window is fixed here and not tuned afterwards.

### 4.2 Net versus gross

Both are reported, always adjacent:

| Figure | Definition |
|---|---|
| Gross recovered | All rupees recovered in the treatment arm |
| **Incremental recovered** | Treatment minus control, per §4 |
| Net incremental | Incremental minus channel costs, discount value, and expected opt-out loss |

**Net incremental is the number that decides whether the system is worth running.** It is the
one that goes in the video.

---

## 5. Secondary metrics

All reported with CIs. All produced by `make evaluate`.

**Money**
- Rupees per contact spent
- Discount value surrendered
- Cost of compliance (§7.3)

**Harm** — reported whether or not it flatters us
- Opt-out rate, treatment vs control, with CI. This is a **causal harm measure**. If Antar
  induces more cancellations than it prevents, that must appear in the README in the same font
  size as the recovery number.
- Contacts per recovered rupee
- Customers contacted with negative estimated uplift (a policy error rate)

**Model quality**
- Qini coefficient and AUUC for each learner
- Calibration plot for the propensity model
- Detection: per-class precision and recall, with false-positive cost denominated in rupees

**Operational**
- Batch solve time
- `UNKNOWN` failure-class rate
- LP infeasibility rate and which constraint bound

---

## 6. Model selection protocol — fixed before the bake-off

### 6.1 Candidates

T-learner, X-learner, R-learner, CausalForestDML, plus two baselines: random targeting and
propensity targeting.

### 6.2 Selection rule

1. Split the exploration data into train / validation by **customer**, 70/30, stratified as §3.3.
2. Fit all four learners with a fixed, pre-specified hyperparameter grid (committed in
   `config/default.yaml` before the run).
3. Select on **validation AUUC**.
4. **Tie-break, in order:** (a) higher Qini at the top 20% of the ranking, (b) narrower bootstrap
   CI on validation AUUC, (c) simpler model, using the order T-learner < X-learner < R-learner <
   CausalForestDML.
5. The selected learner is then, and only then, evaluated against the control holdout.

Step 5 happens once. If the holdout result disappoints, the correct response is to report it,
not to reselect.

### 6.3 Timebox

Per PLAN.md M5: if CausalForestDML has not converged by the morning of 1 Sep, it is dropped from
the candidate set and the drop is recorded as an ADR. Dropping a candidate for a stated
operational reason is fine. Dropping it because it won is not.

---

## 7. Off-policy evaluation

### 7.1 Estimators

IPS, self-normalised IPS, and doubly-robust. DR is the headline; the other two are reported for
comparison so the reader can see how much the estimate depends on the estimator.

### 7.2 Diagnostics — mandatory

- Effective sample size after importance weighting
- Maximum and 99th-percentile importance weight
- Weight-clipping threshold and the fraction of samples clipped

If ESS falls below 10% of nominal, the OPE estimate is reported as **unreliable** and the
on-policy holdout result stands alone. Do not present a DR number with an ESS of 40 as if it
means something.

### 7.3 The consistency check

**DR-OPE estimate of the deployed policy should fall within the CI of the on-policy holdout
result.** If it does not, that is a finding, not an embarrassment: it means either the propensity
model is misspecified or the outcome model is, and either way we investigate and write it up in
`docs/POSTMORTEM.md`. Hiding a disagreement between two estimators is exactly the behaviour this
protocol exists to prevent.

### 7.4 Cost of compliance

From the LP duals: the shadow price of each binding regulatory constraint, converted to rupees
per batch. Reported as "compliance with `TRAI-01` costs this merchant ₹X per 1,000 at-risk cycles
in foregone recovery."

Framed carefully in all artifacts: this is **the price of a rule that exists to protect
consumers**, not an argument against the rule. Get that framing wrong in front of a payments
company and the number becomes a liability rather than an asset.

---

## 8. Three-policy counterfactual replay

Identical batch, identical seed, three policies:

| Policy | Description | Why included |
|---|---|---|
| **P1 — Contact everyone** | Every at-risk cycle gets the default intervention, subject only to hard legal constraints | The industry default; the honest baseline |
| **P2 — Propensity targeting** | Target by predicted probability of recovery | The *sophisticated wrong answer*. Targeting the likely-to-recover is not targeting the persuadable |
| **P3 — Antar** | Uplift estimate + constrained allocation + stopping rules | The system under test |

All metrics from §4 and §5 reported for all three, across all three simulator scenarios. Nine
result columns total.

**The pedagogically important comparison is P2 vs P3**, not P1 vs P3. Beating "contact everyone"
is easy and proves little. Beating a competent propensity-targeting baseline is the claim worth
making, and if we cannot make it, H2 is falsified and we say so.

---

## 9. Robustness

### 9.1 Scenario sensitivity

Every headline result across `conservative`, `base`, and `aggressive`. A sensitivity table, not a
single number.

**If the policy ranking flips under `conservative`, report it prominently.** That would be a real
finding about the regime in which this class of system stops paying for itself, and it is more
interesting than a uniformly positive result.

### 9.2 Seed sensitivity

Five seeds per scenario. Report the across-seed standard deviation of the primary metric. If
seed variance is comparable to the treatment effect, the effect is not established and must be
described that way.

### 9.3 Specification robustness

Vary one at a time, report the effect on the primary metric:
- measurement window: 15 / 30 / 60 days
- control share: 10% / 20% / 30%
- exploration ε: 0.10 / 0.20 / 0.30

---

## 10. Multiplicity and peeking

- **No interim analysis on the control holdout.** One unblinding, at M8.
- Development metrics on the exploration split may be computed freely; they are not inferential.
- Where several secondary metrics are tested, report Benjamini–Hochberg adjusted q-values
  alongside raw p-values, and say which is which.
- For any metric monitored over time in the console, use **always-valid confidence sequences**
  (`antar/eval/sequential.py`) rather than fixed-sample CIs, because a dashboard invites
  continuous looking and fixed-sample intervals are invalid under it.

---

## 11. What would make us withdraw a claim

Specified now so the threshold is not negotiated later.

| Claim | Withdrawn if |
|---|---|
| "Gross overstates incremental" | The gap's CI includes zero in the base scenario |
| "Uplift beats propensity" | P2 and P3 CIs overlap on net incremental rupees per contact |
| "Sleeping dogs exist" | The anti-circularity test fails, or the negative-uplift mass appears in only one scenario |
| "Compliance has a measurable cost" | All regulatory constraints are slack; shadow prices are zero |
| "Detection separates issuer-down from customer-decline" | Per-class recall on `ISSUER_DOWN` is below a pre-set floor of 0.70 |
| Any recovery magnitude as real-world | Always. These are simulated numbers. The words "in simulation" accompany every one. |

---

## 12. Reporting template

The README results section must contain, in this order:

1. The primary metric with CI, base scenario, labelled "in simulation"
2. Gross beside incremental, so the gap is visible without reading prose
3. The three-policy table
4. The scenario sensitivity table
5. Opt-out harm, treatment vs control
6. Hypothesis outcomes — all four, including failures
7. The honest exception list (§12.1)
8. A link to this file and to `docs/SIMULATOR_CARD.md`

### 12.1 The honest exception list

Required content:
- cases where the uplift CI crossed zero and Antar abstained, and how many
- cases where a regulatory constraint blocked a profitable action, and the rupee cost
- the `UNKNOWN` failure-class rate
- LP infeasibility events and how they were handled
- anything the evaluation could not establish

Their published bar for this track says *"one cherry-picked match proves nothing."* This section
is the direct answer to that sentence. Write it before the results section, not after.

---

## 13. Deviations log

Append-only. Every departure from this protocol, with timestamp and reason. Never edit a prior
entry.

| Date | Section | Deviation | Reason |
|---|---|---|---|
| — | — | *(none yet)* | — |

---

## 14. Sign-off

This protocol was written on **22 August 2026**, before any model was fitted and before any
holdout was unblinded. The commit introducing it is the evidence. If that commit has been
rewritten, this document carries no weight and should be treated as a post-hoc narrative.
