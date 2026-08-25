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
| conservative | 3691 | 0.2801 | 0.1496 | 0.3593 | yes |
| base | 3432 | 0.2467 | 0.1533 | 0.3552 | yes |
| aggressive | 3165 | 0.2118 | 0.1592 | 0.3589 | yes |

Failure-class mix, base scenario:

| Failure class | Share |
|---|---|
| INSUFFICIENT_FUNDS | 0.4001 |
| TECHNICAL_DECLINE | 0.1806 |
| AFA_REQUIRED | 0.133 |
| ISSUER_DOWN | 0.1304 |
| RISK_DECLINE | 0.0864 |
| MANDATE_REVOKED | 0.0694 |

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

Selected by the pre-registered rule: **r_learner**

Trained on 1845 rows, validated on 787. Negative-uplift prevalence in validation: 0.66201.


> r_learner: score +0.599 (AUUC 0.8708, negative-region F1 0.469, precision 0.684, recall 0.357). Runner-up x_learner by +0.883. Selected on validation only; the holdout has not been touched.

**Caveat.** Validation AUUC of the selected model is NOT an unbiased estimate of its performance: it won partly on merit and partly on noise, having been chosen on this same split. Only the control holdout is inferential. docs/EVALUATION.md 6.2.2.

Disqualified by the pre-registered floor: `{'causal_forest': 'negative-region recall 0.054 is below the pre-registered floor of 0.1. A model that cannot find the sleeping-dogs population cannot support the claim the system is built on.'}`

## 4. Three-policy comparison, shadow prices, retention

Denominator is **at-risk cycles** (3432 in this batch), not candidates (3148). `docs/EVALUATION.md` 11.2 pre-registered the former; the code divided by the latter until M10 (POSTMORTEM D27).

| Policy | Contacts | Abstentions | Incremental Rs/1k | Opt-out loss Rs/1k | Net Rs/1k |
|---|---|---|---|---|---|
| contact_everyone | 787 | 2361 | 99291.51 | 557832.8 | -458563.35 |
| propensity | 787 | 2361 | 96322.53 | 516556.39 | -420260.91 |
| antar | 338 | 2810 | 164469.33 | 81067.58 | 83367.27 |

**Antar minus propensity targeting: Rs 503628.18 per 1,000 at-risk cycles.** That is the comparison that matters - beating 'contact everyone' is easy.

Where that difference comes from:

| Component | Rs per 1,000 at-risk cycles | Share |
|---|---|---|
| Difference in expected recovery | 68146.8 | 13.5% |
| Difference in avoided cancellation harm | 435488.81 | 86.5% |

**The headline is 86% harm avoidance and 14% extra recovery.** The harm term is priced at an assumed 6x cancellation cost - a config constant, not a measurement - so it scales linearly with an assumption (`docs/LIMITATIONS.md` L18). What does not depend on that assumption at all: Antar recovers Rs 164,469 per 1,000 at-risk cycles against the ranker's Rs 96,323, while sending 338 messages against 787.

Constraint prices:

| Constraint | Price (Rs) | Per 1,000 cycles (Rs) | Method |
|---|---|---|---|
| TRAI-01 contact window (09:00 vs 10:00 opening) | 0.0 | 0.0 | counterfactual_resolve |

Slack (non-binding) rows: contact_capacity. A zero price on a capacity row is a finding, not a null result: at this scenario's opt-out sensitivity the binding constraint is customer tolerance rather than the merchant's outbound capacity, so one more slot is worth nothing. See `docs/LIMITATIONS.md` L14 and the phase diagram for where that changes.

> These are the prices of constraints, including consumer-protection rules. A rule having a cost is not an argument against the rule; it is a fact about this merchant's operation.

Component retention (pre-registered rule, `docs/EVALUATION.md` 12.2):

| Component | Delta net (paise) | 95% CI (paise) | Verdict |
|---|---|---|---|
| downtime_crosscheck | -71033 | (-107835, -42454) | **DELETE** |
| changepoint_detector | -88772 | (-140717, -36828) | **DELETE** |

- `downtime_crosscheck`: Removing it *improves* net incremental recovery by Rs 710. DELETE.
- `changepoint_detector`: Removing it *improves* net incremental recovery by Rs 888. DELETE.

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

- Cells evaluated: **176**
- `x_mean_self_heal`: 0.1 to 0.6 in 11 steps
- `y_mean_optout_sensitivity`: 0.05 to 0.4 in 8 steps

**Antar is ahead in every cell of the pre-registered grid in both panels** (Antar beats propensity targeting in 100% of the grid for a merchant with every channel and 100% for one with only SMS), so the indifference boundary lies at or below the bottom edge of the committed opt-out range (0.05) - outside the space we pre-registered, which is itself the finding. The extension did not locate it either: no opt-out level in the extended range is a decisive win at every self-heal level, so the boundary is below the extension floor and this grid does not say where. Counting only cells outside the indifference band, Antar wins 51% of the multi-channel grid and 52% of the SMS-only one. The two panels are within a few points of each other, so the channel mix does not change how often the advantage is decisive. The magnitudes differ too - median advantage Rs 345,134 vs Rs 391,393 per 1,000 cycles, a 13% difference.

A point estimate from a simulator is worth very little. A boundary condition
derived from one is a genuine contribution, which is why this section exists.

## 7. End-to-end batch and the audit ledger

- Events: **3432**, decided **3432**
- Contacted **542**, abstained **2883**, holdout **697**, refused by the gate **7**
- Simulated recoveries: **463**, opt-outs **225**
- Ledger entries: **14277**
- Ledger head: `e5ebd4003c02a629857c37eac5967583...`
- Chain verifies: **True**
- Replay self-consistent: **True**
- Uplift model: `r_learner-n541`, policy `pol-89656a22a956`

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
