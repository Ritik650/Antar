# LIMITATIONS.md

What Antar cannot do, has not verified, or has assumed. Written to be found, not
buried. A reviewer's objection that is already in here with an honest answer costs us
nothing; one that is not in here costs us everything.

Numbered so they can be cited from the README, the console, and the panel-defence
document.

---

## L1 · The Razorpay integration is structurally tested, not empirically tested

**Status: open. This is the largest gap in the build.**

PLAN.md M1 required recorded payloads from a live test-mode account and predicted
"expect several" divergences between test mode and live behaviour, to be found in
week one rather than week two. No test-mode credentials were available for this
build, so:

- `tests/fixtures/webhooks/*.json` are **synthesised** from public documentation by
  `scripts/make_fixtures.py`, not captured from the wire. See
  `tests/fixtures/README.md`.
- **Zero test-mode divergences are recorded below, and that is not evidence that
  there are none.** It is evidence that nobody looked. The count of known unknowns
  here is the honest reading.
- The client's retry, backoff, circuit-breaker and idempotency behaviour is proven
  against an injected transport (`respx`), which tests our logic and not Razorpay's.

**Closing it:** `scripts/record_fixtures.py` with test-mode keys. It captures real
responses, diffs their field sets against the synthesised fixtures, and prints every
divergence in a form ready to paste into this section. One command, no test changes.

### L1.1 Divergences found so far

| # | Endpoint / event | Divergence | Consequence |
|---|---|---|---|
| — | — | *(none recorded — see L1)* | — |

---

## L2 · Every recovery number is simulated

No production payment data was available, and none of the public alternatives fit
(see `docs/SIMULATOR_CARD.md` section 2). Consequences, stated plainly:

- **External validity for magnitudes is zero.** The rupee figures are properties of
  parameters we chose.
- **External validity for rankings is unknown.** That the uplift policy beats the
  propensity policy in simulation is a claim about the simulator until it is
  replicated on real data.
- The words "in simulation" accompany every recovery figure in every artifact. A
  place where they do not is a bug.

The claims that do survive are about the machinery: that the estimators recover
planted effects, that the allocator respects the encoded constraints, that the system
degrades safely under fault. Those are testable here and are tested.

---

## L3 · The simulator contains no unobserved confounding

By construction, every driver of both treatment and outcome is either observable or
explicitly withheld. Real payment data is not like this. Unmeasured confounding is
the dominant threat to any uplift estimate in production, and our validation says
**nothing** about robustness to it.

Anyone extending this work should add a hidden-confounder mode before trusting these
estimators on real data. `docs/SIMULATOR_CARD.md` section 11 carries the same warning
at more length.

---

## L4 · Regulatory rules are compiled from secondary sources

Every rule in `antar/policy/regulations.py` carries a `citation_url` and a
`verified` flag. Rules that have not been checked against the primary circular are
marked `ADVISORY` and are reported as unverified in
`docs/REGULATORY_REGISTER.md`, which is generated from the code so the two cannot
drift apart.

A confident wrong claim about RBI rules in front of a payments panel is worse than an
acknowledged gap, so the default for anything unverified is to say so.

---

## L5 · Single-mandate customers only

The simulator gives each customer exactly one subscription. Real merchants have
customers with several, and the contact budget (`C-BUDGET`) should be shared across
them — three messages per customer per 30 days, not three per mandate. Antar's budget
accounting is per customer already, so the model is right; it is simply never
exercised against a multi-mandate customer.

---

## L6 · No festival seasonality, no partial payments, no chargebacks

Named because each is real and material in India and none is modelled:

- Diwali and other festival-period spending shifts move both failure rates and
  balance curves
- Partial payments and payment plans are a common real recovery path
- Chargebacks and disputes arising from aggressive recovery are a genuine cost that
  our cost model does not carry, which **flatters** the treatment arm

`docs/SIMULATOR_CARD.md` section 11 has the full list.

---

## L7 · Two detection components do not currently pay for themselves

> **SUPERSEDED by L13 on 2026-08-23.** The end-to-end measurement this section called
> for now exists, and both components were **deleted** by it. The section is kept
> unedited below because the open question was recorded before the answer was known,
> and rewriting it afterwards would erase the ordering that makes the answer credible.

**Reported because it is against us.** `artifacts/detection_base.json` contains a
leave-one-out ablation of the detection layer, scored in rupees on the control arm:

| Configuration | Wrong-action cost |
|---|---|
| Full system | ₹98,837 |
| Full **minus** the changepoint detector | ₹76,337 |
| Full **minus** the downtime cross-check | **₹64,410** |

Under the action-cost matrix in `antar/detect/root_cause.py`, both the Downtime API
cross-check and the EWMA/CUSUM changepoint detector make the system *more* expensive,
not less. The mandate FSM contributes nothing at all at first failure (correctly — it
cannot know about a cancellation that has not happened yet). Only the classifier
clearly earns its place, cutting cost by roughly 6x.

**Why.** Both components push ambiguous events toward `ISSUER_DOWN`, which recommends
`WAIT`. But `UNKNOWN` already recommends `WAIT`, so on the events they resolve they
change the label without changing the action — while the events they get *wrong* turn
a recoverable `TECHNICAL_DECLINE` into a wait, which the matrix prices at 0.60x the
cycle.

**The honest caveat on the caveat.** This ablation scores L2 *in isolation*, and that
understates the components' value, because the expensive consequence of contacting
during an outage is not in this matrix at all — it is in the L3 objective, where an
induced opt-out is priced at `decide.optout_loss_multiplier` (6x) the cycle amount.
The number to trust is the end-to-end one from `make evaluate`, not this one.

**Status: open.** The end-to-end comparison is the deciding measurement and it does
not exist yet. Until it does, the claim "the downtime cross-check earns its place" is
**not supported by anything measured**, and this document says so rather than the
README implying otherwise.

---

## L8 · The failure classifier trains on labels that would not exist in production

In this build the classifier's labels come from the simulator's ground truth
(restricted to the treatment arm, so no control event is used for fitting). A real
merchant has no such column. They would have to construct labels from eventual outcome
plus manual review of a sample — slower, noisier, and subject to its own selection
effects, since the cases a human bothers to review are not a random sample.

Every per-class precision and recall figure Antar reports should be read as an
upper bound for this reason.

---

## L9 · Changepoint detection has little power at simulated volume

The simulated merchant produces roughly 5 attempts per issuer×method segment per day.
A four-hour outage therefore contains about one attempt, and the detector reports
**26 of 212** windows detected, with a median delay of 2.6 hours.

That is a property of the simulated volume, not of the algorithm: a real merchant with
thousands of daily attempts per segment would give the same detector far more to work
with. It does mean the changepoint numbers in this build say little about how the
component would perform in production, in either direction.

---

## L11 · Antar honours no per-customer communication preferences

`TRAI-08`. Beyond the default-OFF time bands, TCCCPR Schedule II lets a customer
register preferences on specific two-hour bands, on days of the week, and on public and
national holidays. Those narrow the permitted window below the 10:00–21:00 default.

**Antar consumes no preference feed**, so none of them is honoured. The rule is encoded
as `ADVISORY` rather than `BLOCKING` for exactly that reason: marking it blocking would
claim a compliance check the system does not perform.

A production deployment must consume the preference feed before any of this can be
called compliant. What Antar demonstrates is the *mechanism* — that a registered
preference would compile into a per-candidate constraint and remove infeasible actions
before the solver sees them — not a working compliance posture.

Related: `TRAI-05` (number series) can never be exercised because Antar places no real
calls, and no template here is registered with an access provider.

---

## L12 · Three regulatory rules rest on secondary sources

Of 18 encoded rules, **3 are `ADVISORY` solely because we could not reach a primary
document**: `TRAI-02` (message classification), `TRAI-05` (number series), `TRAI-06`
(DND scope). They are reported and priced but cannot block an action, enforced by
`Regulation.__post_init__` rather than by convention.

This is the honest state, not a target. Reaching the TRAI gazette PDF for the
definitional clauses would move at least `TRAI-02` and `TRAI-06` to `BLOCKING` and
would change the constraint set.

Verification did find six errors in PLAN.md's own statement of the rules — see
`docs/REGULATORY_REGISTER.md` § Corrections. The most material: the contact window
opens at **10:00**, not 09:00.

---

## L10 · The LLM path is off by default

`act.llm.enabled` defaults to `false`, so the drafter uses the deterministic template
filler unless an `ANTHROPIC_API_KEY` is present and the flag is set. Every draft
produced this way is marked `fallback_used=true` in the audit trace, so a run's
figures never silently imply an LLM was involved when it was not.

The adversarial suite exercises both paths: injections are tested against the real
schema-validation and contamination logic using recorded model outputs, so the
defences are tested even when the network is not.

---

## L13 · Two detection components were deleted by their own measurement

**Resolved, against ourselves.** `docs/EVALUATION.md` §12.2 was committed before the
allocator existed and bound us to a rule. Measured on 2026-08-23, base scenario, 3 seeds:

| Component | Δ net per 1,000 cycles | 95% CI | Verdict |
|---|---|---|---|
| Downtime API cross-check | ₹0.00 | [0, 0] | **DELETE** |
| EWMA/CUSUM changepoint detector | **−₹846.76** | [−2,540, 0] | **DELETE** |

Both are now off in `config/default.yaml`, and
`tests/statistical/test_component_retention.py` fails if the configuration and the
recorded verdict ever disagree — so re-enabling either requires a new measurement, not
an edit.

**What deletion cost.** Detection quality got worse, and that is reported rather than
omitted: accuracy when resolved fell 92.4% → 91.7%, `ISSUER_DOWN` recall fell
0.82 → 0.70, and the wrong-action cost rose ₹140k → ₹153k on the control arm. Net money
improved by ₹847 per 1,000 cycles. That is the trade the pre-registered rule made, with
its eyes open.

**Why they did not earn their place.** Once the L3 objective prices the RBI-EM-02
opt-out hazard directly, the allocator declines outage-affected candidates on economics
without needing to be told the issuer was down. The changepoint detector was worse than
redundant: its false alarms vetoed contacts the allocator correctly wanted to make.

**What this does not say.** It does not say a downtime feed is useless in general. It
says that *in this objective, at this scenario's opt-out sensitivity*, it is. A merchant
whose customers tolerate contact — the low-opt-out region of the phase diagram — has a
binding contact capacity, and there the feed would have something to contribute. The
measurement is regime-specific and is reported as such.

Supersedes L7, which recorded the open question.

---

## L14 · Every capacity shadow price is zero in the base scenario

The LP duals on the contact-capacity row come out at **₹0**, and the counterfactual
price of the TRAI-01 contact window is likewise **₹0 per 1,000 cycles**.

This is a finding, not a null result. At the base scenario's opt-out sensitivity the
binding constraint is **not** the merchant's outbound capacity — it is customer
tolerance. Antar contacts 23 of 574 eligible candidates and declines slots it is fully
entitled to use, so one more slot is worth nothing, and an extra hour of window in which
to use slots it does not want is worth nothing either.

Two honest consequences:

- **The headline "one more contact slot is worth ₹X" demo does not exist in the base
  scenario.** The truthful version is "₹0 — and here is why, and here is the regime
  where it becomes positive." The phase diagram reports the share of the grid in which
  capacity binds.
- **The window price is also limited by our model.** The contact window is modelled as a
  *scheduling* constraint (when an action may fire) rather than a *throughput* one (how
  many may fire per hour). A merchant whose outbound capacity is per-hour would lose
  roughly a eleventh of their daily throughput to Note-1; we do not model per-hour
  throughput, so we cannot price that, and we do not claim to.

---

## L15 · The contamination classifier is thin; the rules do the work

`contamination.py` is described as a hybrid — deterministic rules for the obvious,
a classifier for phrasings nobody wrote a rule for. Measured against the seven positive
cases in `tests/adversarial/test_contamination.py`:

| Half | Blocks |
|---|---|
| Deterministic rules | **7 of 7** |
| `LexicalClassifier` score alone, at the 0.50 threshold | **3 of 7** |

On the five lawful negative cases the classifier scores 0.00 on all five, so it costs
nothing in false positives — but it is not currently carrying the half of the load the
design attributes to it.

The reason is structural. `LexicalClassifier` keys on promotional-register vocabulary,
which makes it a keyword list wearing a different hat: it inherits the same blind spot as
the rules it is meant to back up. D19 is the demonstration — "twenty percent off annual
plans" scored **0.00**, missing for exactly the reason the regex missed.

**What we claim, therefore.** The detector blocks every case in the suite, and the
deterministic half is why. The classifier is a genuine second path — it blocks
`"an exclusive premium deal reward"`, which trips no rule — but it is a stand-in for a
learned model, not a learned model, and it should be read as defence-in-depth with an
untested depth rather than as an independent detector. A real classifier is future work;
substituting one is a drop-in through the `classifier` parameter, which is why the
seam exists.

**Not fixed by widening the lexicon.** Adding "annual plan" and its neighbours would
make the table look better and change nothing about the argument, because the next
evasion would be phrased in words that are not in the wider list either. The honest
statement is that we have one strong path and one weak one, not two strong ones.

---

## L16 · Channel choice is made on cost, not on effect

The uplift learner is trained on **treated versus untreated**. It produces one estimate
per event and has no opinion about which channel to use, because the training design
never asked it that question.

So `pipeline._build_candidates` picks the **cheapest feasible contact channel**. That is
the honest reading of an estimator that cannot distinguish channels, and it is a much
weaker claim than the architecture might suggest. Antar does not know that SMS beats
WhatsApp for a particular customer. It knows that contacting beats not contacting, by
how much, and that SMS costs less.

`eval/policies.py` does choose the channel on expected value — but from ground truth, in
the oracle comparison, where that is legitimate and labelled. The production path cannot
and does not.

**What it would take to fix.** A per-channel treatment indicator in the exploration
design, so the learner sees `(treated, channel)` rather than `treated`. The exploration
policy already randomises channel (ADR-0015 draws treatment first, then a channel), so
the data exists; the feature builder and the learner interface do not currently carry it.
That is a design change rather than a tuning change, and it landed outside the M8 scope.

---

## L17 · The opt-out cost is a causal effect only by construction

`OptoutRisk` estimates the harm term in L3's objective. It is **not** an uplift learner,
and it cannot be one on this data.

Base scenario, seed 7:

| Arm | Opt-outs | Rows | Rate |
|---|---|---|---|
| Untreated | 0 | 2,996 | 0.000 |
| Treated | 35 | 260 | 0.135 |

The control arm has a single class. There is nothing to difference. This is a property
of `SIMULATOR_CARD.md`'s response model, which represents opt-out purely as a hazard
triggered by contact — spontaneous cancellation is not modelled at all, and real
customers cancel subscriptions on Sunday afternoons for reasons no merchant caused.

`OptoutRisk` therefore fits `P(optout | X, treated)` on the treated arm and subtracts the
**measured** untreated rate rather than assuming it is zero. On this data the subtraction
is a no-op. The code is written so that a simulator with spontaneous churn, or real data,
would be estimated properly without changing the estimator.

**The claim this permits, and the one it does not.** We may say: *in this simulator, the
opt-out cost Antar prices is the full causal effect of contacting.* We may not say: *this
is how you would estimate opt-out harm in production.* In production the untreated rate
is not zero, and the difference between the two rates — not the treated rate — is the
number that belongs in the objective.

**Direction of the error, if we are wrong.** Overstating the untreated baseline would
make contacts look *safer* than they are. Our baseline is measured, and measured at zero,
so Antar prices opt-out harm at its maximum defensible value here. The bias, if any,
is toward contacting less than optimal — which is the side of this particular error we
would choose.
