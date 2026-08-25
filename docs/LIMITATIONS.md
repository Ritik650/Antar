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
allocator existed and bound us to a rule. Measured on 2026-08-25, base scenario,
5 seeds, per 1,000 **at-risk cycles** (the pre-registered denominator; see D27):

Δ is `with − without`, so a negative number means the component was costing money:

| Component | Δ net per 1,000 cycles | 95% CI | Verdict |
|---|---|---|---|
| Downtime API cross-check | **−₹284.35** | [−429.04, −130.76] | **DELETE** |
| EWMA/CUSUM changepoint detector | **−₹243.09** | [−451.75, −66.62] | **DELETE** |

Both intervals exclude zero, on the side that says the components were *costing* money
rather than merely failing to earn their place. The pre-registered rule would have
deleted them either way — ambiguity resolves to DELETE — but this is the stronger
finding, and it is the one that survives.

**This supersedes the M6 measurement**, which reported ₹0.00 [0, 0] for the cross-check
and −₹846.76 [−2,540, 0] for the changepoint detector. That run was valid when it was
made. The verdict has not changed. What changed is that the ablation was rebuilding its
*with-component* arm from the shipped configuration, so once M6's own verdict switched
these flags off, both arms described the same detector and every later delta was exactly
zero — a rule that could only ever re-confirm itself. POSTMORTEM D25, ADR-0028.

Both are now off in `config/default.yaml`, and
`tests/statistical/test_component_retention.py` fails if the configuration and the
recorded verdict ever disagree — so re-enabling either requires a new measurement, not
an edit.

**What deletion cost.** Detection quality got worse, and that is reported rather than
omitted: accuracy when resolved fell 92.4% → 91.7%, `ISSUER_DOWN` recall fell
0.82 → 0.70, and the wrong-action cost rose ₹140k → ₹153k on the control arm. Net money
improved by ₹284 and ₹243 per 1,000 cycles respectively. That is the trade the
pre-registered rule made, with its eyes open.

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

## L17 · The opt-out contrast is measured, but spontaneous churn is still unmodelled

**Superseded in part on 25 Aug 2026.** This entry used to say the opt-out cost was "a
causal effect only by construction", because no untreated customer ever opted out. That
was not a property of the design — it was a hard-coded `return 0.0` (POSTMORTEM D28), and
calling it a limitation stopped anyone looking for the bug.

After the pre-registered amendment in `docs/EVALUATION.md` §3.3, both arms have a real
hazard. Base scenario, seed 7:

| Arm | Opt-outs | Rows | Rate |
|---|---:|---:|---:|
| Untreated | 170 | 2,996 | **0.0567** |
| Treated | 35 | 260 | **0.1346** |
| **Causal effect** | | | **+0.0779** |

`OptoutRisk` now subtracts a non-zero measured baseline, and `value_of` prices the
**incremental** harm rather than the level (POSTMORTEM D32).

**What is still true, and is the real limitation.** The control hazard is
*notification-driven*: it models a customer opting out because the mandate's own
pre-debit notice carried a cancel route. It does **not** model spontaneous cancellation —
a customer who cancels on a Sunday afternoon for reasons no merchant caused. Real
baseline churn is out of scope, so the measured contrast above is still an upper bound on
the harm attributable to contact.

**The constant that sets the baseline is chosen, not measured.**
`BASELINE_INTRUSIVENESS = 0.40` against SMS at 1.00. It was pre-registered before the
re-run so it was not chosen to flatter, but nothing calibrates it. Halving it roughly
doubles the measured causal effect.

---

## L18 · 86% of the headline is a harm term scaled by an assumed constant

The primary result — Antar minus propensity targeting, **₹503,628 per 1,000 at-risk
cycles** — decomposes as:

| Component | Per 1,000 at-risk cycles | Share |
|---|---:|---:|
| Difference in expected recovery | ₹68,147 | 14% |
| Difference in avoided opt-out loss | ₹435,489 | **86%** |

The harm term is:

```
expected_optout_loss = optout_uplift x amount x optout_loss_multiplier
```

where `decide.optout_loss_multiplier = 6.0`.

**That 6.0 is an assumption.** It asserts that an induced cancellation costs six cycles of
revenue. It is a crude lifetime-value proxy, chosen before any result existed — the only
thing that recommends it — and it scales 86% of the headline **linearly**.

**Improved since the first version of this entry.** The split used to be 0.6% recovery /
99.4% harm, because the harm term was priced at the *level* of the opt-out hazard rather
than its uplift over an assumed-zero baseline (D28, D32). Correcting that moved recovery
from a rounding error to 14% of the result.

| Quantity | Status |
|---|---|
| Opt-out uplift given a contact | Measured contrast between two non-zero arms |
| `BASELINE_INTRUSIVENESS = 0.40` | **Assumed.** Sets the control hazard. |
| The 6x multiplier | **Assumed.** Not calibrated against anything. |
| Amount at risk | Simulated, calibrated against published figures |
| Recovery advantage at lower contact volume | **Measured, and independent of both assumptions** |

**The claim that survives without any multiplier.** Antar recovers
₹164,469 per 1,000 at-risk cycles against the
ranker's ₹96,323 — 71% more — while sending
338 messages against 787. No multiplier appears in that sentence.

**Direction of the sensitivity.** The phase diagram varies the *probability* side of the
harm product; the multiplier is the other half and has never been varied. A specification
curve over it is the obvious next piece of work and is not in this build.


---

## L19 · The integration is real; the recovery action has never run against it

**Executed on 25 Aug 2026** against a Razorpay test-mode account.
`python tasks.py roundtrip` → `artifacts/razorpay_roundtrip.json`, committed.

| Call | Result |
|---|---|
| `GET /payments/downtimes` | ok — the feed L1 consumes for `ISSUER_DOWN` |
| `POST /orders` | ok |
| `POST /orders` with the **same idempotency key** | ok, **and it returned the same order** |
| `POST /payment_links` | ok, with `notify.sms` and `notify.email` forced false |
| `GET /payments/{unknown}` | **deliberate 400**, to capture the real error envelope |
| `GET /orders/{id}` | ok |

**6 of 7 calls succeeded.** The one failure is the intended one.

**What this establishes.** Auth, the idempotency header, request signing, response parsing
and the error path all work against the live API rather than against a fixture. The
idempotency result is the load-bearing one: `PolicyGate` treats a retried action as a
replay rather than a second charge, and that assumption is now checked against Razorpay
instead of assumed. Ids were compared in memory and are not recorded.

**The real error envelope**, which is the field set `antar/signals/razorpay_errors.py`
claims to parse:

```json
{"code": "BAD_REQUEST_ERROR", "source": "internal",
 "step": "payment_initiation", "reason": "input_validation_failed"}
```

Those four field names are now confirmed observed, not inferred from documentation.

**What it does NOT establish, and this is the half that matters.** **`charge_mandate` has
never been executed.** A test-mode mandate charge needs an authenticated subscription
where a customer has completed an e-mandate flow, which is a manual step no script can do
unattended. So the single endpoint this project's thesis is about — retrying a failed
recurring debit — is exercised only against synthesised fixtures (ADR-0007).

**Consequence for the error taxonomy.** The round trip confirms the *envelope*. It does not
sample the *population* of error codes a live merchant sees, because it never provoked a
real decline. `razorpay_errors.py` maps codes to failure classes from Razorpay's published
documentation, and the `UNKNOWN` rate of 0.0 in detection is a simulator property for
exactly this reason.

**Two defects the round trip found, which is the argument for running it.** Two calls in
`record_roundtrip.py` were written against signatures that did not exist
(`create_order(receipt=...)`, `create_payment_link` without `customer`) and failed in 0 ms
without leaving the process — the client was right and the caller was wrong, and no
fixture would have shown that. Razorpay then rejected the first contact number with
*"Recurring digits in customer contact are disallowed"*, a validation rule that appears in
no documentation we had read.

**Honest position.** The integration is tested and narrow. The recovery numbers are
simulator output. The gap between those two statements is this entry, and the README says
so in its opening block rather than here.
