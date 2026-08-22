# Antar

**A causal revenue-recovery controller for Indian recurring payments.**

Razorpay AI Buildathon 2026 · Track 03 — AI Revenue Recovery

> Gross recovery is a vanity metric. Antar measures **incremental** recovery against a
> randomised holdout, and treats customer outreach as a scarce, regulated resource to
> be allocated by constrained optimisation rather than a message to be sent.

---

## Scope tiers

Pinned per PLAN.md section 17. Not renegotiated after M5.

| Tier | Contents | Rule |
|---|---|---|
| **T0** | One loss class end-to-end · detection · one uplift learner · constrained allocator · gated execution · audit ledger · holdout evaluation with CIs | If not done by end of M6, everything below is cancelled |
| **T1** | Second loss class · four-learner bake-off · DR-OPE · LP with shadow prices · adversarial suite · chaos suite · three-policy replay | These are what win. Budgeted, not hoped for |
| **T2** | Retry scheduling as a finite-horizon DP · contextual-bandit exploration · third loss class as config · OpenTelemetry tracing | Cut without regret |

---

## Status

Build in progress. This README carries no results yet, and will carry none until
`make evaluate` produces them. Every number that eventually appears here is generated
by a committed script; anything that cannot be generated is written as
**not measured**.

Every recovery figure Antar reports is produced inside a simulator whose generative
assumptions are documented in [docs/SIMULATOR_CARD.md](docs/SIMULATOR_CARD.md). The
words "in simulation" accompany every one of them, without exception.

---

## Quickstart

```bash
docker compose up            # api, worker, postgres, redis, console
```

or, with no Docker:

```bash
pip install -e ".[dev]"
python tasks.py evaluate     # identical to `make evaluate`
```

## Repository map

| Path | What lives there |
|---|---|
| `antar/signals/` | L1 — Razorpay client, webhook receiver, downtime, the inter-layer schemas |
| `antar/simulator/` | The event generator and the ground-truth response model |
| `antar/detect/` | L2 — failure taxonomy, changepoint detection, mandate FSM. **No LLM** |
| `antar/decide/` | L3 — uplift estimation, LP allocation, scheduling, stopping rules. **No LLM** |
| `antar/policy/` | Regulations as data, the constraint compiler, and the PolicyGate |
| `antar/act/` | L4 — templates, LLM drafter, contamination detector, executors |
| `antar/audit/` | L5 — hash-chained ledger, decision traces, replay |
| `antar/eval/` | The pre-registered evaluation protocol, in code |
| `docs/` | Architecture, decisions, simulator card, evaluation protocol, limitations |

## Documents worth reading before the code

- [PLAN.md](PLAN.md) — the build specification
- [docs/EVALUATION.md](docs/EVALUATION.md) — the pre-registered protocol, committed before any model was fitted
- [docs/SIMULATOR_CARD.md](docs/SIMULATOR_CARD.md) — what the simulator assumes and what it cannot tell you
- [docs/DECISIONS.md](docs/DECISIONS.md) — the ADR log

## Licence

MIT.
