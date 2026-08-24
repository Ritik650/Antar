# Model card

Antar contains **four** models. They do different jobs, have different failure modes, and
deserve to be described separately rather than as "the model".

| Model | Job | Where |
|---|---|---|
| Failure classifier (L2) | Which of six failure classes caused this decline? | `antar/detect/classifier.py` |
| Recovery uplift (L3) | How much does contacting *increase* recovery probability? | `antar/decide/uplift/learners.py` |
| Opt-out risk (L3) | How much does contacting *increase* cancellation probability? | `learners.py::OptoutRisk` |
| Contamination classifier (L4) | Is this drafted message promotional? | `antar/act/contamination.py` |

The LLM is **not** in this table. It fills one text slot and makes no decision (N1).

> Every performance figure here is measured on **simulated** data
> (`docs/SIMULATOR_CARD.md`). None is a production metric.

---

## 1. Failure classifier (L2)

**What it does.** Maps a Razorpay error code plus context to one of `INSUFFICIENT_FUNDS`,
`ISSUER_DOWN`, `MANDATE_REVOKED`, `AFA_REQUIRED`, `TECHNICAL_DECLINE`, `RISK_DECLINE`, or
`UNKNOWN`, with a confidence and a recommended intervention class.

**Why it is not just a lookup table.** It partly is — a documented code maps directly, and
the table path handles the majority of events. The classifier exists because roughly a
quarter of codes are *ambiguous*: `BAD_REQUEST_ERROR` covers at least three genuinely
different situations, and disambiguating needs context the code does not carry.

**Inputs.** Error code, reason, source, step; amount; method; issuer; merchant category;
segment failure rate; mandate state; attempt number. **Never** any simulator latent —
enforced by `tests/statistical/test_no_leakage.py`, which exists because an early version
read the answer key directly (POSTMORTEM **D10**).

**Performance** (base scenario, control arm only — never used for fitting):

| | |
|---|---|
| Accuracy when resolved | 0.917 |
| Total cost of wrong actions | ₹153,456 |
| Best | `MANDATE_REVOKED` precision 1.000, recall 0.880 |
| Worst | `RISK_DECLINE` precision 0.688, recall 0.710 |
| Most expensive error | `ISSUER_DOWN`, ₹70,162 |

**Read the cost column, not the accuracy.** Antar's cost matrix is denominated in
*actions*, not labels (**ADR-0008**): confusing two classes that lead to the same action
costs nothing, and confusing two that lead to opposite actions costs the full value of the
cycle. Misreading `ISSUER_DOWN` is expensive because it converts "wait, the bank is down"
into "contact the customer about a failure that was never theirs".

**Known limitation.** The `UNKNOWN` rate is **0.0**, which is a fact about the simulator
before it is a fact about the classifier. The generator draws from the documented Razorpay
taxonomy plus a deliberately unmapped minority; a real error stream contains far more
vendor variants and post-dated codes. Treat 0.0 as a lower bound on production
(`docs/LIMITATIONS.md`).

**Deleted components.** Two detection components — a downtime cross-check and a changepoint
detector — were removed by a pre-registered retention rule that found neither earned its
place in rupees. Detection got measurably worse and net money got better. Both halves are
reported (**L13**, **ADR-0018**).

---

## 2. Recovery uplift (L3)

**What it does.** Estimates the conditional average treatment effect of contacting on
recovery probability: `P(recover | contacted, X) − P(recover | not contacted, X)`.

**Architecture.** X-learner over gradient-boosted base learners. Selected from four
candidates (T-learner, X-learner, R-learner, causal forest) by a rule pre-registered in
`docs/EVALUATION.md` §6.2 before any of them were fitted.

**Training data.** The exploration slice only — events where the treatment decision was
randomised, so propensities are **known by construction** rather than estimated. The
randomised holdout is never in the training set, enforced by a test that fails if a
control `event_id` appears in any fitted frame (N3).

**Selection result.** `x_learner`, AUUC 0.978, negative-region sign F1 0.236.

**The finding that matters more than the selection.** *No learner beats a trivial "always
abstain" predictor on negative-region sign F1* — 0.261 against the best learner's 0.254.
Sign recovery at the bottom of the ranking is precisely where the sleeping-dog thesis
lives, and no model here demonstrates it convincingly on that metric. A rupee-denominated
abstention-value metric does discriminate, and on it `r_learner` would have won.

We kept `x_learner` because the rule was fixed before the numbers existed
(**ADR-0016**, POSTMORTEM **D15**). Reporting this is the point: the alternative is a
project that silently re-selects on whichever metric flatters it.

**What the estimate is used for, exactly.**

```
net = recovery_uplift × amount − channel cost − optout_uplift × amount × multiplier
```

That number is the LP objective coefficient. It is **not** used to rank and fill a
capacity — a candidate whose net is negative is not selected even when the budget is
empty, which is the difference between this and a propensity ranker.

**Calibration and intervals.** `uplift_ci` on each decision is a fixed half-width from the
exploration-slice standard error, **not** a per-event posterior. The field name says `ci`
and the trace prints it; this card is where the caveat lives.

**What it cannot do.** It has no opinion about *which channel* to use — it was trained on
treated-versus-not, so channel choice falls back to cost (**L16**).

---

## 3. Opt-out risk (L3)

**What it does.** Estimates the increase in cancellation probability caused by contacting.
This is the harm term, and it is why some perfectly compliant actions are worth less than
doing nothing.

**Architecture.** A **treated-arm response model** (gradient-boosted classifier) minus the
measured untreated rate. Deliberately not an uplift learner.

**Why not an uplift learner.** It cannot be fitted here. Base scenario, seed 7:

| Arm | Opt-outs | Rows | Rate |
|---|---:|---:|---:|
| Untreated | 0 | 2,996 | 0.000 |
| Treated | 35 | 260 | 0.135 |

The control arm has one class. There is nothing to difference. That is a property of the
simulator's response model, which represents opt-out purely as a contact-triggered hazard
— real customers cancel for reasons no merchant caused.

**The claim this permits, and the one it does not.** *In this simulator, the opt-out cost
Antar prices is the full causal effect of contacting.* Not: *this is how you would estimate
opt-out harm in production.* There the untreated rate is not zero, and the difference — not
the treated rate — is what belongs in the objective. Full statement in **L17**.

**One deliberate asymmetry.** A predicted *increase* in opt-out is charged as a cost; a
predicted *decrease* is clamped to zero rather than credited. Letting the model pay for a
contact by claiming it retains customers is a claim this design cannot support and has
every incentive to make.

---

## 4. Contamination classifier (L4)

**What it does.** Decides whether a drafted message is promotional. Under TRAI-03,
promotional content mixed into a transactional message reclassifies the **whole** message,
changing the required header and number series and bringing DND into scope.

**Architecture.** Hybrid, rules-first. Ten deterministic constructions, then
`LexicalClassifier` over promotional-register vocabulary. A rule match pins the score at
1.0 and no classifier confidence can lower it (**ADR-0022**).

**Performance** on the adversarial suite:

| Half | Blocks (of 7 positives) | False positives (of 5 negatives) |
|---|---:|---:|
| Deterministic rules | **7** | 0 |
| Classifier alone, at threshold | **3** | 0 |

**Honest assessment: the classifier is thin** (**L15**). It keys on vocabulary, which makes
it a keyword list wearing a different hat, and it inherits the same blind spot as the rules
it is meant to back up. POSTMORTEM **D19** is the demonstration — "twenty percent off
annual plans" evaded the regex *and* scored 0.00 on the classifier. Read the hybrid as one
strong path and one weak one, not two strong ones. The `classifier` parameter is a seam so
a real model can be dropped in.

---

## Versioning and reproducibility

Every `Decision` records `model_version` (e.g. `x_learner-n554`, the learner plus its
training-set size) and `policy_version` (a content hash over all eighteen regulations, the
contact window, and the budgets). Every `DraftedMessage` records `prompt_version`, a
content hash of the system prompt and the template.

A decision whose model cannot be identified is not reproducible, so `antar/audit/replay.py`
refuses to compare across a version change silently: `decision_id` is derived from the
versions and is therefore deliberately excluded from replay's comparison set, so that a
version bump shows up as a version bump rather than as a false divergence
(**ADR-0025**).

---

## Failure modes, and what happens

| Failure | Behaviour |
|---|---|
| No fitted uplift model | **Refuses to act.** `NoModel` is raised; there is no path that continues. A uniform score under a capacity constraint is "contact everyone until the budget runs out" (**ADR-0027**). |
| Classifier throws | Degrades to the deterministic rules; the message is still evaluated. |
| LLM unreachable or malformed | Deterministic template fallback, flagged in the trace with the repair count (**D23**). |
| LP infeasible | Documented greedy fallback, with the offending constraint recorded. |
| Ledger does not verify | Replay refuses; the console shows a red banner and disclaims the trace. |

---

## What Antar should not be used for

- **Deciding who to lend to, price to, or refuse service to.** It estimates the effect of
  a *message*, on a customer who already has a contract, about a payment they already owe.
- **Any population unlike the simulated one.** The generator models Indian recurring
  payments across six merchant categories and three instruments. Nothing here transfers to
  one-off checkout, cross-border, or B2B invoicing without re-calibration.
- **Production, today.** It has never seen a real customer. The gate defaults to dry-run
  and the LLM defaults to off, and both of those defaults are correct for what this is.
