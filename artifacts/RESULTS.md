# Results

> **Every number on this page is simulated.** It comes from the generator
> described in `docs/SIMULATOR_CARD.md`, calibrated against published Indian
> recurring-payments figures. None of it is a measured recovery rate from a
> live merchant, and none of it should be quoted as one. This is N6.

This file is written by `python tasks.py evaluate` from the JSON artifacts in
`artifacts/`. Nothing here is typed by hand. The README quotes this file.

Mode: **full**. Scenario: **base**.

## 1. Does the simulator match reality?

| Scenario | At-risk events | First-attempt failure rate | Above AFA ceiling | Ambiguous codes | Taxonomy clean |
|---|---|---|---|---|---|
| conservative | 3705 | 0.2821 | 0.1466 | 0.2524 | yes |
| base | 3436 | 0.2472 | 0.1531 | 0.2494 | yes |
| aggressive | 3210 | 0.2174 | 0.1579 | 0.2595 | yes |

Failure-class mix, base scenario:

| Failure class | Share |
|---|---|
| INSUFFICIENT_FUNDS | 0.405 |
| TECHNICAL_DECLINE | 0.1829 |
| AFA_REQUIRED | 0.1312 |
| ISSUER_DOWN | 0.1286 |
| RISK_DECLINE | 0.0837 |
| MANDATE_REVOKED | 0.0687 |

## 2. Detection (L2)

Evaluated on the **control arm (never used for fitting)** (661 events).

- Accuracy when resolved: **0.9168**
- UNKNOWN rate: **0.0**
- The UNKNOWN rate is a property of the generator as much as of the detector. The simulator draws error codes from the documented Razorpay taxonomy plus a deliberately unmapped minority (27 of 661 events here); a real error stream contains far more vendor variants and post-dated codes. Read this figure as a lower bound on the UNKNOWN rate Antar would see in production, not as evidence that the mapping is complete.
- Total cost of wrong actions: **Rs 153456.32**

| Failure class | Precision | Recall | Support | Cost of wrong action (Rs) |
|---|---|---|---|---|
| AFA_REQUIRED | 0.9574 | 0.9783 | 46 | 0.0 |
| INSUFFICIENT_FUNDS | 0.9 | 0.9122 | 148 | 31403.87 |
| ISSUER_DOWN | 0.7609 | 0.7 | 50 | 70161.75 |
| MANDATE_REVOKED | 1.0 | 0.88 | 25 | 0.0 |
| RISK_DECLINE | 0.6875 | 0.7097 | 31 | 13128.3 |
| TECHNICAL_DECLINE | 0.9533 | 0.9612 | 361 | 38762.4 |

## 3. Uplift bake-off (L3)

Selected by the pre-registered rule: **x_learner**

Trained on 1845 rows, validated on 787. Negative-uplift prevalence in validation: 0.14994.


> x_learner: score +0.912 (AUUC 0.9777, negative-region F1 0.236, precision 0.173, recall 0.373). Runner-up r_learner by +0.705. Selected on validation only; the holdout has not been touched.

**Caveat.** Validation AUUC of the selected model is NOT an unbiased estimate of its performance: it won partly on merit and partly on noise, having been chosen on this same split. Only the control holdout is inferential. docs/EVALUATION.md 6.2.2.

## 4. Three-policy comparison, shadow prices, retention

Denominator is **at-risk cycles** (3432 in this batch), not candidates (3148). `docs/EVALUATION.md` 11.2 pre-registered the former; the code divided by the latter until M10 (POSTMORTEM D27).

| Policy | Contacts | Abstentions | Incremental Rs/1k | Opt-out loss Rs/1k | Net Rs/1k |
|---|---|---|---|---|---|
| contact_everyone | 787 | 2361 | 99291.51 | 1277180.85 | -1177911.4 |
| propensity | 787 | 2361 | 97474.84 | 1093712.41 | -996264.14 |
| antar | 156 | 2992 | 103796.92 | 66750.09 | 37025.24 |

**Antar minus propensity targeting: Rs 1033289.38 per 1,000 at-risk cycles.** That is the comparison that matters - beating 'contact everyone' is easy.

Where that difference comes from:

| Component | Rs per 1,000 at-risk cycles | Share |
|---|---|---|
| Difference in expected recovery | 6322.08 | 0.6% |
| Difference in avoided cancellation harm | 1026962.32 | 99.4% |

**The headline is not a recovery number.** It is overwhelmingly a harm-avoidance number, and that harm is priced at an assumed 6x cancellation cost - a config constant, not a measurement. See `docs/LIMITATIONS.md` L18. The comparison that survives without it is the contact count: 156 against 787.

Constraint prices:

| Constraint | Price (Rs) | Per 1,000 cycles (Rs) | Method |
|---|---|---|---|
| TRAI-01 contact window (09:00 vs 10:00 opening) | 0.0 | 0.0 | counterfactual_resolve |

Slack (non-binding) rows: contact_capacity. A zero price on a capacity row is a finding, not a null result: at this scenario's opt-out sensitivity the binding constraint is customer tolerance rather than the merchant's outbound capacity, so one more slot is worth nothing. See `docs/LIMITATIONS.md` L14 and the phase diagram for where that changes.

> These are the prices of constraints, including consumer-protection rules. A rule having a cost is not an argument against the rule; it is a fact about this merchant's operation.

Component retention (pre-registered rule, `docs/EVALUATION.md` 12.2):

| Component | Delta net (paise) | 95% CI (paise) | Verdict |
|---|---|---|---|
| downtime_crosscheck | -28435 | (-42904, -13076) | **DELETE** |
| changepoint_detector | -24309 | (-45175, -6662) | **DELETE** |

- `downtime_crosscheck`: Removing it *improves* net incremental recovery by Rs 284. DELETE.
- `changepoint_detector`: Removing it *improves* net incremental recovery by Rs 243. DELETE.

## 5. The same question, asked every defensible way

- Specifications evaluated: **540**
- Median negative-uplift share: **0.095** (IQR 0.06 to 0.16833, range 0.01667 to 0.30333)
- Share clearing the pre-registered 0.05 bar: **0.8315**
- The pre-registered specification sits at percentile **21.7** of the curve

Variance in the headline explained by each analytic choice:

| Choice | Variance share |
|---|---|
| action | 0.05444 |
| negative_definition | 0.02763 |
| reference_instant | 0.01132 |
| seed | 0.0048 |
| measurement_window_days | 0.00297 |

> Across 540 analytic specifications, the base negative-uplift share has a median of 9.50% (IQR 6.00%-16.83%), and 83% of specifications clear the pre-registered 5% bar. The pre-registered specification sits at the 21.7th percentile of that distribution.

## 6. Where the result holds, and where it stops

- Cells evaluated: **264**
- `x_mean_self_heal`: 0.1 to 0.6 in 11 steps
- `y_mean_optout_sensitivity`: 0.05 to 0.4 in 8 steps

**Antar is ahead in every cell of the pre-registered grid in both panels** (Antar beats propensity targeting in 100% of the grid for a merchant with every channel and 100% for one with only SMS), so the indifference boundary lies at or below the bottom edge of the committed opt-out range (0.05) - outside the space we pre-registered, which is itself the finding. A disclosed extension below 0.05 locates it: the advantage becomes decisive at an opt-out sensitivity of 0.035 in both panels. Counting only cells outside the indifference band, Antar wins 83% of the multi-channel grid and 75% of the SMS-only one: below the boundary a multi-channel merchant already sees decisive gains in places, while an SMS-only merchant sees none. The magnitudes differ too - median advantage Rs 971,262 vs Rs 1,346,912 per 1,000 cycles, a 39% difference - the SMS-only merchant gaining more, because a propensity ranker with a single channel has fewer ways to be accidentally right.

A point estimate from a simulator is worth very little. A boundary condition
derived from one is a genuine contribution, which is why this section exists.

## 7. End-to-end batch and the audit ledger

- Events: **3432**, decided **3432**
- Contacted **215**, abstained **3217**, holdout **697**, refused by the gate **0**
- Simulated recoveries: **493**, opt-outs **16**
- Ledger entries: **13943**
- Ledger head: `926c9bd83b058e35477f6aab7923b771...`
- Chain verifies: **True**
- Replay self-consistent: **True**
- Uplift model: `x_learner-n541`, policy `pol-89656a22a956`

The ledger head is the hash of the last entry. Publishing it is what makes a
truncation detectable from outside - see ADR-0024.


---

## Reproducing this

```
git clone <repo> && cd antar
python tasks.py install
python tasks.py evaluate
```

Every stage is deterministic given `run.seed` in `config/default.yaml`.
`python tasks.py evaluate QUICK=1` runs the same code on smaller batches.
