# Antar

**A causal revenue-recovery controller for Indian recurring payments.**
It decides *whether* to contact a customer, not just *how*.

Razorpay AI Buildathon 2026 — Track 03.

> **Everything below is simulated.** No live merchant data was used. The generator is
> documented in [`docs/SIMULATOR_CARD.md`](docs/SIMULATOR_CARD.md) and calibrated against
> published Indian recurring-payments figures. No number here is a measured recovery
> rate, and none should be quoted as one.

---

## The problem

A UPI Autopay mandate fails. A card gets declined. An e-mandate hits the ₹15,000 AFA
ceiling. The standard response is a dunning sequence: retry, then message, then message
again, then discount.

That sequence has a hidden assumption — **that contacting a customer can only help.**

It cannot. Some customers were going to pay anyway; a reminder spends money and goodwill
to buy nothing. Some were quietly lapsed and a reminder is what makes them notice, cancel
the mandate, and take the whole subscription with them. In the uplift literature these
are *sure things* and *sleeping dogs*, and every recovery system that ranks customers by
"likelihood to recover" targets both of them enthusiastically.

Antar asks a different question. Not *who is likely to pay?* but **who pays *because* we
contacted them?** — and, just as importantly, *who cancels because we did?*

---

## The thesis, in one table

Three policies, same batch, same contact capacity, same simulator:

| Policy | Contacts | Abstentions | Expected recovery | Expected opt-out loss | **Net per 1,000 cycles** |
|---|---:|---:|---:|---:|---:|
| Contact everyone | 787 | 2,361 | ₹340,768 | ₹4,383,285 | **−₹1,284,178** |
| Propensity targeting | 787 | 2,361 | ₹334,534 | ₹3,753,621 | **−₹1,086,143** |
| **Antar** | **156** | **2,992** | ₹356,231 | **₹229,086** | **+₹40,366** |

**Antar minus propensity targeting: ₹1,126,509 per 1,000 at-risk cycles.**

Read the middle column, not the last one. Antar recovers *more* money than the propensity
ranker while contacting **a fifth as many people**, and it does that by declining to
contact the customers a ranker most wants to reach. The opt-out column is where the
difference lives, and it is the column most recovery dashboards do not have.

Beating "contact everyone" is easy and proves nothing. The comparison that matters is the
second row.

*(Source: `artifacts/allocation_base.json`, reproduced by `python tasks.py evaluate`.)*

---

## What makes it a controller rather than a model

```
 L1 signals    Razorpay webhooks, downtime feed, error taxonomy
      |        antar/signals/
      v
 L2 detect     WHY did it fail? Six failure classes, cost-weighted.
      |        antar/detect/          -> ISSUER_DOWN recommends WAIT, not CONTACT
      v
 L3 decide     WHO benefits, and by how much? X-learner CATE, then an LP
      |        antar/decide/          that maximises net rupees under constraints
      v
 L4 act        Registered templates, slot-filled by the LLM. Every money
      |        antar/act/             action through PolicyGate. No bypass.
      v
 L5 audit      Hash-chained, append-only. Every decision replayable.
               antar/audit/
```

The constraints are not a filter bolted on afterwards. They are **rows in the linear
program**, which is what lets Antar report the *shadow price* of each one — what the
10:00–21:00 contact window costs this merchant, what one more contact slot would be
worth. A rule having a price is not an argument against the rule; it is a fact about the
merchant's operation.

Eighteen regulations are encoded as data with primary-source citations
([`docs/REGULATORY_REGISTER.md`](docs/REGULATORY_REGISTER.md)). Verifying them against
primary sources rather than secondary summaries corrected six of them — most materially,
the TRAI contact window opens at **10:00**, not 09:00, because Schedule II Note-1 makes
the 08:00–10:00 band default-off.

---

## Results

Full detail in `artifacts/RESULTS.md`, which is **generated from the JSON artifacts** by
`python tasks.py evaluate`. Nothing in it is typed by hand.

### Detection (L2), evaluated on the control arm only

| | |
|---|---|
| Accuracy when resolved | **0.917** |
| Cost of wrong actions | ₹153,456 |
| Best class | `MANDATE_REVOKED`, precision 1.000 |
| Worst class | `RISK_DECLINE`, precision 0.688 |

The cost column matters more than the accuracy figure. Misreading `ISSUER_DOWN` costs
₹70,162 because it means contacting a customer about a failure that was never theirs.

The `UNKNOWN` rate is **0.0**, and that is a property of the simulator as much as the
detector — see the caveat in `RESULTS.md`. It is a lower bound on what production would
show, not evidence the mapping is complete.

### Uplift (L3)

Four learners, one pre-registered selection rule, selected **`x_learner`** (AUUC 0.978,
negative-region sign F1 0.236).

**The uncomfortable finding is reported, not buried.** No learner beats a trivial "always
abstain" predictor on negative-region sign F1 — 0.261 against the best learner's 0.254.
A better criterion would have selected `r_learner`. We kept `x_learner` because the rule
was fixed before the numbers existed and re-opening it after seeing them is how
pre-registration dies ([ADR-0016](docs/DECISIONS.md), POSTMORTEM D15).

### Does the headline survive other analytic choices?

**540 specifications.** Median negative-uplift share **9.5%** (IQR 6.0%–16.8%). **83%**
clear the pre-registered 5% bar. Our pre-registered specification sits at the **21.7th
percentile** — near the conservative end of the distribution, which is the direction you
want to be wrong in.

### Where does the result stop holding?

A point estimate from a simulator is worth very little. A **boundary condition** from one
is a contribution.

**264 cells**, self-heal probability × opt-out sensitivity, two channel-mix panels. Antar
wins every cell of the pre-registered grid — so a disclosed post-hoc extension below the
committed floor was needed to locate the boundary, at an **opt-out sensitivity of ≈0.035**
([ADR-0020](docs/DECISIONS.md)). Below that, contact capacity binds; above it, the scarce
resource is customer tolerance and every capacity shadow price is **₹0**. That is why the
"one more slot is worth ₹X" demo does not exist here, and we say so
([L14](docs/LIMITATIONS.md)) rather than quoting a number from a regime we did not measure.

### Two components were deleted by their own measurement

A retention rule was pre-registered before the allocator existed:

Per 1,000 at-risk cycles, across 5 seeds. **Δ is `with − without`**, so a negative
number means the component was *costing* money:

| Component | Δ net (with − without) | 95% CI | Verdict |
|---|---:|---|---|
| `downtime_crosscheck` | −₹311.45 | (−₹467.56, −₹144.28) | **DELETE** |
| `changepoint_detector` | −₹264.36 | (−₹491.89, −₹72.80) | **DELETE** |

Both intervals exclude zero, on the side that says these components were not merely
unproven but actively harmful: their false alarms vetoed contacts the allocator correctly
wanted to make. The pre-registered rule would have deleted them either way — ambiguity
resolves to DELETE — but this is the stronger version of the finding.

Both were deleted, and detection got measurably *worse* as a result — accuracy
92.4% → 91.7%, `ISSUER_DOWN` recall 0.82 → 0.70. Both halves are reported
([L13](docs/LIMITATIONS.md)). Deleting a working component because your own measurement
says it does not earn its place is the only part of this that was difficult.

---

## Quickstart

```bash
git clone <repo> && cd antar
python tasks.py install

python tasks.py evaluate        # every number above, from scratch (~40 min)
python tasks.py evaluate QUICK=1  # same code, smaller batches (~10 min)

python tasks.py simulate        # one batch end to end, writes the audit ledger
python tasks.py console         # the operator console
python tasks.py gate            # lint + the full test suite
```

`make` works as an alias for every target. No API keys are needed: the LLM is **off by
default** and the deterministic template path is the default draft path. Nothing in the
default configuration can send a message — `gate.dry_run` is true and no Razorpay client
is wired in.

To use real Razorpay test-mode data:

```bash
export RAZORPAY_KEY_ID=rzp_test_...     # refuses to run against a live key
export RAZORPAY_KEY_SECRET=...
python tasks.py seed-test-mode
```

---

## The console answers one question

> *"Why did you contact this customer at 11:04 on a Tuesday?"*

One click, one paragraph, assembled entirely from the hash-chained ledger:

> A payment of Rs 20,313.00 for cust_001571 failed with code BAD_REQUEST_ERROR. L2
> classified it as AFA_REQUIRED at 97% confidence via table, and recommended CONTACT. L3
> estimated an uplift of +0.3072 (95% CI +0.2572 to +0.3572) and chose VOICE scheduled
> for 2026-04-02T19:20:00+05:30. […] The gate approved VOICE and the run is in dry-run,
> so it was deliberately not sent.

Nothing in the console recomputes a decision. A console that recomputes is a second
implementation of the decision path, and when the two disagree the operator cannot tell
which one is lying. This one can only show what was recorded, which makes a screenshot of
it evidence.

---

## Honest limitations

The full list is [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) — seventeen entries. The
four that most affect how you should read this README:

1. **It is a simulator.** Calibrated, documented, and still a simulator. Every rate on
   this page is generated. ([SIMULATOR_CARD.md](docs/SIMULATOR_CARD.md))
2. **Channel choice is made on cost, not effect** ([L16](docs/LIMITATIONS.md)). The
   learner is trained on treated-versus-not and has no opinion about SMS versus WhatsApp.
   Antar knows contacting beats not contacting; it does not know which channel is better
   for you.
3. **The opt-out cost is a causal effect only by construction** ([L17](docs/LIMITATIONS.md)).
   No untreated customer in the simulator ever opts out — 0 in 2,996, against 35 in 260
   treated — so the control arm has one class and there is nothing to difference. Real
   customers cancel on Sunday afternoons for reasons no merchant caused.
4. **The contamination classifier is thin** ([L15](docs/LIMITATIONS.md)). The TRAI-03
   detector blocks 7 of 7 adversarial cases; the deterministic rules do all of that work
   and the classifier clears threshold on 3 of 7. It is defence-in-depth with an untested
   depth.

---

## What went wrong while building this

[`docs/POSTMORTEM.md`](docs/POSTMORTEM.md) has 26 entries, each with the defect, the root
cause, the fix, and — where it matters — the order in which things were discovered. It is
the most useful document in the repository. Four of them:

- **D10** — the detector was reading `batch.true_failure_class`. Recall 1.000, because it
  had the answer key. A leak travels by data as easily as by import.
- **D21** — `self.ledger = ledger or NullLedger()`. `Ledger` defines `__len__`, so an
  *empty* ledger is falsy, and every caller's ledger was silently discarded on the one run
  that matters: the first.
- **D22** — the pipeline recorded the model's estimate and acted on the simulator's. Both
  halves of "L3 estimated +0.3072 and chose VOICE" were true; the connective was a lie.
- **D24** — a headline number in an artifact was the changepoint detector's retention
  effect, in paise, under a key ending `_rupees`. It survived two milestones because no
  test compared a derived number to the components it came from. One does now.
- **D25** — the pre-registered retention rule stopped being able to measure anything, by
  succeeding. Once its own verdict switched the components off, both arms of the ablation
  described the same detector and every delta was exactly zero. A rule that could only
  re-confirm itself.

---

## Repo map

| Path | What lives there |
|---|---|
| `antar/signals/` | L1 — webhooks, downtime, the Razorpay error taxonomy, all schemas |
| `antar/detect/` | L2 — failure classification, mandate FSM, changepoint detection |
| `antar/decide/` | L3 — features, four uplift learners, OPE, the LP allocator |
| `antar/policy/` | Regulations as data, the constraint compiler, `PolicyGate` |
| `antar/act/` | L4 — templates, the drafter, executors, sagas, TRAI-03 detection |
| `antar/audit/` | L5 — hash-chained ledger, trace assembly, deterministic replay |
| `antar/eval/` | Holdout, three-policy comparison, phase diagram, specification curve |
| `antar/simulator/` | The generator. Read `SIMULATOR_CARD.md` before trusting any number |
| `antar/pipeline.py` | All five layers in sequence — the path the ledger records |
| `tests/` | unit · property · integration · chaos · adversarial · statistical |
| `docs/` | Decisions, limitations, postmortem, evaluation protocol, clock audit |
| `artifacts/` | Everything `tasks.py evaluate` produces, including `RESULTS.md` |

---

## Non-negotiables this build kept

| | |
|---|---|
| **N1** | The LLM writes language only. It fills one slot; it never chooses an action, an amount, or a recipient. |
| **N2** | Every money action goes through `PolicyGate`. There is no bypass path, including in tests — enforced by an introspective coverage test that discovers the executors rather than listing them. |
| **N3** | The randomised holdout is never used for training and never acted on. |
| **N4** | `python tasks.py evaluate` reproduces every number in this README. |
| **N5** | Regulations are data, with citations, verified against primary sources. |
| **N6** | Simulated rates are never presented as real ones. |

---

## Licence and provenance

Built for the Razorpay AI Buildathon 2026. The simulator, the regulations register, and
every result here are original to this repository. Secrets are read from environment
variables only; a pre-commit scan rejects anything matching `rzp_live_*`, `rzp_test_*`,
`sk-ant-*`, `AKIA*`, or a private-key block.
