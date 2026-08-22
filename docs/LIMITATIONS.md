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

## L7 · The LLM path is off by default

`act.llm.enabled` defaults to `false`, so the drafter uses the deterministic template
filler unless an `ANTHROPIC_API_KEY` is present and the flag is set. Every draft
produced this way is marked `fallback_used=true` in the audit trace, so a run's
figures never silently imply an LLM was involved when it was not.

The adversarial suite exercises both paths: injections are tested against the real
schema-validation and contamination logic using recorded model outputs, so the
defences are tested even when the network is not.
