# Panel defence

Ten questions from PLAN.md §15. Each answer is one page, and each names something that
cost us. Question 7 is the one to rehearse.

---

## 1 · Why Track 03 and not the agentic-commerce track?

Because Track 03 has a **ground truth we can be wrong about**, and agentic commerce
mostly does not.

A recovery decision produces an observable outcome: the customer paid or did not, opted
out or did not, within a measurement window. That makes it possible to build a randomised
holdout, run an off-policy evaluation, and pre-register a metric that can come back
against you. An agentic checkout demo is judged on whether the demo works.

The second reason is that the interesting question in recovery is **causal**, and causal
questions are where an LLM-first architecture is most likely to be confidently wrong.
That gave the project a thesis rather than a feature list. See ADR-0001.

---

## 2 · Why a simulator instead of a public dataset? What would you do with real data?

**No public dataset contains what this needs.** The requirement is joint observation of a
failed recurring payment, a contact decision, *and* whether the customer subsequently
cancelled — with enough randomisation to identify a causal effect. Public payment
datasets have failures without interventions. Public uplift datasets (Criteo, Hillstrom,
Lenta) have randomised interventions in retail marketing, with no mandate, no regulator,
and no cancellation hazard.

Building the simulator was also the only way to have negative-uplift customers whose
ground truth is *known*, so a claim about them can be checked rather than asserted.

**And it is what let the claim be refuted.** Our pre-registered test for a reportable
negative-uplift population failed after the D38 correction, and we could only discover
that because the simulator makes the true uplift observable. On real data the claim would
have been unfalsifiable and we would still be asserting it.

**With real data, in order:**

1. **Run the holdout for real.** The 20% control arm is already in the code and is never
   trained on. It is the whole design.
2. **Re-fit the opt-out model against real spontaneous churn.** Since D28 both arms have
   a real hazard — control 0.0567 against treated 0.1346, a measured effect of +0.0779 —
   but the control hazard is still *notification-driven*. It does not model a customer
   cancelling on a Sunday afternoon for reasons no merchant caused, so the measured
   contrast remains an upper bound on harm attributable to contact (L17).
3. **Calibrate the 6× cancellation multiplier**, which currently scales 86% of the
   headline and is an assumption (L18).
4. **Re-run the phase diagram** to find out which regime the merchant is actually in.

---

## 3 · Why no LLM in the decision layer? Where *would* you add one?

Because the decision layer's output is money, and its inputs are regulations that are
predicates. An LLM adds nothing to `is this inside the 10:00–21:00 window` and adds a
failure mode.

Concretely, N1 is enforced mechanically rather than by policy: registered templates own
every message body, `DraftContext.fixed_slots()` supplies every amount, date and URL from
the `Decision`, and the model is asked for exactly one slot — a short human phrase
explaining the failure. Invented keys are discarded before rendering. A successful prompt
injection is a non-event: persuade the model to emit `{"amount": "10000"}` and the
rendered message still carries the amount the allocator decided.

**Where an LLM would genuinely help, and is not here:**

- **Error-taxonomy maintenance.** Roughly a quarter of Razorpay codes are ambiguous and
  new vendor variants appear continuously. An LLM proposing taxonomy mappings, with a
  human approving them into `taxonomy.py`, is real leverage.
- **Multilingual drafting.** Ten registered templates in English is the honest limit of
  what one person can verify. Language coverage is where slot-filling pays.
- **Reading the regulations.** We verified eighteen rules against primary sources by
  hand. An LLM doing first-pass extraction with a human confirming citations would have
  saved days — and would have needed the same `BLOCKING`-requires-primary-verification
  guard the code already has.

Each is *upstream* of the decision, produces artefacts a human approves, and cannot move
money.

---

## 4 · Why this uplift learner over the other three? Show me the bake-off.

`artifacts/bakeoff_base.json`. Four learners — T-learner, X-learner, R-learner, causal
forest — against a selection rule pre-registered in `docs/EVALUATION.md` §6.2 before any
of them were fitted. **R-learner wins**: AUUC 0.871, negative-region sign F1 **0.469** at
0.684 precision.

**The winner changed, and the rule did not.** Until two days ago this was X-learner at
sign F1 0.246 — *worse* than a trivial "always abstain" predictor's 0.261, which we
reported rather than hid (ADR-0016, D15). Fixing the control-arm opt-out defect (D28)
changed the data underneath the bake-off, and the same unchanged rule then selected
R-learner, whose 0.469 clears the abstain baseline comfortably.

Be precise about this if pressed: **we did not re-open the rule after seeing results.**
The results moved because a bug was fixed, and the rule was allowed to say what it says.
`pipeline.UPLIFT_MODEL` is asserted equal to the bake-off's selection by a test, so the
shipped model cannot drift from the procedure that chose it.

Still honest about the remainder: recall is 0.357, so roughly two in three negative-uplift
customers are missed.

If you push on this: **the model is not the contribution.** Swap in any CATE estimator;
the architecture, the constraints and the measurement discipline are what the project is
about — and the fact that the winner changed under a fixed rule is itself the evidence
that the rule, not the model, is doing the work.

---

## 5 · Why an LP rather than a greedy heuristic? What do the duals buy you?

Three things a heuristic cannot give.

**Abstention as a first-class option.** A greedy ranker fills its budget top-down. It has
no way to express "this candidate is worth *less than nothing*" — and in our base
scenario Antar contacts 338 of 3,148 candidates, leaving budget unused because the
remaining candidates have negative value. That is the entire result.

**Coupled constraints.** Contacts-per-customer-per-30-days and the margin budget span
candidates. Greedy handles those by rejecting after the fact, which is not the same
allocation.

**The duals.** We solve twice — a MILP for the selection a merchant would act on, and an
LP relaxation for the shadow prices, because an integer programme has no duals and
quoting one from a rounded solution would be making it up (ADR-0019).

**What the duals actually bought us here is a null result, and it is the best finding in
the build.** Every capacity shadow price in the base scenario is **₹0** — because Antar
declines slots it is entitled to use. The scarce resource is not outbound capacity but
customer tolerance. That is what identifies the two regimes in the phase diagram, and it
is a statement no greedy heuristic could have produced.

There *is* a documented greedy fallback for infeasibility, it records the offending
constraint, and `tests/chaos/` exercises it.

---

## 6 · Where did these regulatory constraints come from and how do you know they are current?

`antar/policy/regulations.py` — eighteen rules as data, each a pure predicate on
`DecisionContext` with a primary-source citation and a verification level. Eleven are
`BLOCKING`, and the constructor **refuses to construct** a `BLOCKING` rule resting on a
secondary source.

Sources: the RBI e-mandate framework and its AFA circulars, TRAI's TCCCPR 2018 with the
2025 Second Amendment, and the DLT registration regime.

**How we know: we checked, and it cost us.** Verifying against primary sources rather
than secondary summaries **corrected six rules**. The most expensive: PLAN.md assumed the
contact window opens at 09:00, from a secondary summary. TCCCPR Schedule II para 3(1)
Note 1 makes the 08:00–10:00 band default-off for every customer. It opens at **10:00**.
That cost an hour of daily contact capacity and raised our own shadow prices, and we
priced the hour rather than quietly keeping the wider window.

**How we would keep them current:** each rule carries its citation and verification date,
`docs/REGULATORY_REGISTER.md` is generated from the code so it cannot drift, and the
`policy_version` hash over all eighteen rules is recorded in every decision — so a
regulation change makes every prior decision identifiably from a different regime.

**What we do not claim:** this is not legal advice, the DLT template IDs are
correctly-formatted placeholders and none is actually registered (L11), and a real
deployment registers each body verbatim.

---

## 7 · What in your own system do you not trust?

**Rehearse this one hardest.**

The expected answer is "the simulator". It is true, and it is the safe answer, and it
describes a limitation we designed in and documented.

**The real answer is that our own safeguards kept failing in one specific way, and the
most important instance was found by a reviewer rather than by us.**

Every control in this build points at the *inputs* to numbers — no leakage, a randomised
holdout that never trains, pinned clocks, regulations verified against primary sources, a
gate no money action can bypass. That was not enough, repeatedly:

- **D24** — a headline field held a different quantity, in the wrong unit, for two
  milestones.
- **D28** — the simulator gave the control arm a **zero** opt-out hazard under a comment
  saying control customers receive the notification that carries the opt-out route, and a
  test enforced the zero as an invariant.
- **D38** — the treated arm was discounted by its survival probability and the untreated
  arm was not. **That asymmetry biased the project's motivating finding toward the
  project's own thesis.**

**Be direct about D38, because it is the one worth respecting.** D33 had already recorded
that asymmetry as known and deliberately unfixed, reasoning that correcting it "would move
numbers in Antar's favour". That reasoning was backwards — removing it *shrinks* the
negative-uplift population, which is the thesis. So the asymmetry sat open across three
review cycles while easier work got done, and it was an external reviewer who quantified
it.

**And when we fixed it, the claim died.** The pre-registered test needed a negative-uplift
share of 5% in 2 of 3 scenarios. Post-correction: conservative 0.2309, base
0.0267, aggressive 0.0002. One of three. **The sleeping-dogs claim is
withdrawn**, the specification curve's median fell from 9.5% to 4.33%, and clearance
from 83% to 46%.

**D39, found while cleaning up after D38.** A test asserted `verdict.supported` — it
asserted our own hypothesis, so disproving it turned the build red. A test that fails when
your hypothesis fails is a standing incentive to keep the hypothesis true, and it is
invisible for exactly as long as the finding is favourable.

**What I do not trust, stated precisely:** guards that encode what we expect to find
rather than that the finding was measured correctly. We have now shipped three
(D28's test, D39's assertion, and D33's reasoning) and caught them only afterwards.

**What I would build next:** a rule that no test may assert the *value* of a pre-registered
outcome — only that it was computed, recorded, and enforced. That is now true of
`test_anti_circularity.py`, and it should be a lint, not a habit.

**If they push: what still stands?** Antar recovers ₹167,137 per 1,000 at-risk
cycles against the ranker's ₹112,481 — 49% more — on 342 contacts against
787. That result never depended on uplift being negative. It depends on pricing the
harm of a contact at all, which is a population-level argument.

---

## 8 · What breaks first at 10,000 merchants?

**The LP, and not for the reason people expect.** One binary per candidate solves fine at
batch scale — 3,148 candidates in seconds. At 10,000 merchants the problem isn't size,
it's that a *shared* solve couples merchants who have nothing to do with each other. The
fix is per-merchant solves, which is embarrassingly parallel; the interesting part is
that the contact-capacity row is per-merchant already, so the decomposition is clean.

**Second: the ledger.** SQLite with a full-table scan for verification (ADR-0006). We
already hit the wall in miniature — `build_trace` re-verified the whole chain per event
and a 14,000-entry batch looked like a hang (D26). At 10,000 merchants this needs
per-merchant chains with a published head each, and periodic verification rather than
on-read.

**Third, and the one that actually matters: the model.** One X-learner fitted across all
merchants assumes the treatment effect transfers between an OTT subscription and a mutual
fund SIP. The phase diagram says it does not — those merchants are in different regimes
and need opposite systems. What breaks is not throughput, it is the assumption that one
model is appropriate, and the phase diagram is already the evidence.

**What does not break:** the gate, the regulations (pure predicates, trivially parallel),
and the drafter.

---

## 9 · What would you build with three more months?

In priority order, each addressing something measured rather than imagined.

1. **A real mandate charge against the Razorpay sandbox (L19).** `tasks.py roundtrip`
   proves the client, auth, idempotency and error parsing work, but `charge_mandate` has
   never run — and that is the endpoint the whole thesis is about. It needs an
   authenticated e-mandate, which is a manual step, not a hard one.
2. **Recover the sleeping-dogs claim, or abandon it properly.** It failed at 5% in 2 of 3
   scenarios. The honest next step is to find out whether that is a property of India's
   recurring-payments population or of our opt-out hazard, and only real data answers it.
3. **Per-channel treatment effects.** The learner is trained on treated-versus-not, so it
   has no opinion about which channel to use and channel choice falls back to *cost*
   (L16). The exploration policy already randomises channel — the data exists; the
   feature builder and learner interface do not carry it.
4. **A specification curve over the opt-out multiplier.** It scales 86% of the headline
   and has never been varied (L18). The phase diagram varies the probability side of that
   product; the multiplier is the other half.
5. **A real contamination classifier.** The TRAI-03 detector's rules block 7 of 7
   adversarial cases and the "classifier" clears threshold on 3 of 7 — it is a lexicon,
   which inherits the same blind spot as the rules it backs up (L15). The seam exists.
6. **The dispersion guard from question 7.**
7. **A real merchant pilot**, in dry-run, comparing Antar's decisions against what the
   merchant's existing dunning actually did — no sending, just disagreement analysis.
   That is the cheapest possible reality check and needs no holdout.

**What I would not build:** a better uplift model. Model quality is not the
differentiator, and PLAN.md's anti-goals say so.

---

## 10 · Where does this overlap with Razorpay Agent Studio, and where is it complementary?

**Overlap: almost none, and deliberately.** Agent Studio is for building agents that take
actions through tools. Antar's central architectural claim is that **the language model
gets no tool access at all** — it fills one text slot, downstream of every decision that
matters. Building this *as* an agent would remove the property that makes it defensible.

**Complementary in three specific places:**

1. **Antar as a tool an agent calls.** A merchant-support agent asking "should we chase
   this customer?" gets back a decision, a constraint list, and a trace. Antar is the
   thing an agent should ask, not the agent.
2. **The trace as agent-readable explanation.** `narrate()` already produces the paragraph
   a support agent would need to answer "why was I contacted?" — assembled from the
   ledger, not generated.
3. **The gate as a pattern.** `@requires_gate` plus a coverage test that *discovers*
   money-moving functions rather than listing them is directly reusable for any agent
   framework that touches money. The interesting part isn't the gate; it's that nobody
   has to remember to register with it.

**The honest framing:** this is the layer an agent platform needs *underneath* it for
money decisions, and the reason it is not itself agentic is the same reason a payment
processor is not a chatbot.


---

## The four they will actually press on

Not questions from the list. These are the weak points a reviewer identified as the ones
worth attacking, and all four are documented — which is not the same as being ready to say
them out loud. Rehearse these as answers, not as admissions.

### "Your simulator's external validity is zero for the magnitudes."

**Agreed, and stated in the README's first paragraph.** No number here transfers. What
transfers is the *ordering* of the three policies under a harm price, and the boundary
condition in the phase diagram — a statement about which regime a merchant is in, which
they can locate for themselves from their own opt-out rate. If pressed further: the
correct use of this work is the architecture and the evaluation protocol, and the
magnitudes are there to prove the protocol can produce a number at all.

### "There is no unobserved confounding, by construction. That flatters every method."

**True, and it flatters ours as much as anyone's** — which is the honest form of the
answer. Our exploration arm is randomised, so propensities are *known* rather than
estimated, and every method in the bake-off gets that advantage. On real data the
propensity model would be estimated with error and every uplift estimate would degrade,
ours included. What it does not flatter is the *allocation* argument: abstaining when
expected harm exceeds expected benefit does not depend on identification at all.

### "The arm asymmetry was caught externally, not by your guards."

**Correct, and it is the most useful thing anyone told us.** Every guard pointed at inputs
(leakage, clocks, regulations) or provenance (numbers trace to artifacts). None asked
whether the treatment and control arms were handled symmetrically — a category of error
where the code is internally consistent and the *comparison* is not. It sat open across
three review cycles while easier work got done, and the reasoning in D33 actively
protected it by getting the direction of bias backwards.

Do not soften this. The follow-up worth volunteering: **fixing it withdrew our motivating
claim**, and we published that.

### "No real money has ever moved through this system."

**None, and here is exactly how far it did get.** `artifacts/razorpay_roundtrip.json` is
committed: 6 of 7 live test-mode calls, including `POST /orders` followed by the
**same idempotency key returning the same order** — the property `PolicyGate` relies on to
make a retry a replay rather than a second charge, now checked against Razorpay instead of
assumed. A deliberate 400 captured the real error envelope, confirming `code`, `source`,
`step` and `reason` are the field names we parse rather than names we inferred.

**`charge_mandate` has never run.** It needs an authenticated e-mandate a script cannot
create unattended (L19), so the one endpoint the thesis is about is fixtures only, and the
error taxonomy is documented rather than observed.

**Volunteer the embarrassing part.** Running it found two bugs in our own script — calls
written against signatures that did not exist — and a Razorpay validation rule
("Recurring digits in customer contact are disallowed") that appears in none of the
documentation we had read. That is the argument for having run it at all, and it is a
better answer than a clean result would have been.

The gate defaults to `dry_run` and the LLM defaults to off. Both are correct for a system
that has never been supervised in production.

**The line to close on if they push all four at once:** every one of these is in the
repository, in writing, before you asked. The project's claim is not that it is finished —
it is that you can tell exactly how far it got.
