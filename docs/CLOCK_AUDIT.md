# CLOCK_AUDIT.md

Every site in `antar/` that reads the authoritative clock, classified.

## Why this document exists

`docs/POSTMORTEM.md` D13: the pre-registered anti-circularity gate flipped across a
midnight with no code change, because the population scan evaluated itself at
`clock.now()`. The generalisation in that entry is the useful part:

> A discipline rule that names a **mechanism** (`datetime.now()`) rather than a
> **property** (determinism) leaves a gap exactly where someone obeys it literally.

`tests/unit/test_clock.py` forbids `datetime.now()` outside `antar/clock.py`. The buggy
code obeyed that rule perfectly — it called `clock.now()`. The rule was written to stop
*scheduling* drifting with a skewed worker clock, and it never occurred to its author
that a *measurement* would read the clock at all.

So the property is now stated directly, and audited:

> **No statistical claim's value may depend on the wall-clock instant at which it is
> computed.**

## The two categories

Every clock read is one of:

- **SCHEDULING** — legitimately time-dependent. "Is now inside the TRAI contact window?"
  is a question whose answer *should* change with the clock. These are correct as they
  are and must not be pinned.
- **MEASUREMENT** — feeds a number that gets reported. These must be pinned to an
  explicit reference instant. A reported figure that moves overnight is not a
  measurement.

The dangerous third category is the one D13 fell into: a clock read that *looks* like
scheduling (it produces a timestamp for a scheduled action) but flows into a feature and
then into a reported number.

## The audit

| Site | Category | Verdict |
|---|---|---|
| `policy/gate.py:224` — `now = clock.now()` | SCHEDULING | Correct. The gate decides caps and lead times against real time; that is its job. |
| `detect/root_cause.py:337` — `diagnosed_at` | SCHEDULING | Correct. A provenance stamp on an audit record. Written, never read into a metric. |
| `detect/changepoint.py:202` — report fallback timestamp | SCHEDULING | Correct. Used only when a tracker has no observations; cosmetic in a report header. |
| `signals/downtime.py:121` — `prune_resolved` cutoff | SCHEDULING | Correct. Retention of stale windows is genuinely a function of now. |
| `signals/downtime.py:172` — `begin` fallback | SCHEDULING | Correct. A Downtime entity with no `begin` is assumed to have started now. |
| `signals/webhook_receiver.py:335` — `created_at` fallback | SCHEDULING | Correct. A payload with no timestamp is stamped on arrival. |
| `signals/razorpay_client.py:421` — `utc_epoch` | SCHEDULING | Correct. Builds outbound request timestamps. |
| `eval/claims.py` — population scan | **MEASUREMENT** | **Was wrong (D13). Pinned** to `SCAN_REFERENCE`. |

**Result: one measurement site, and it was the bug.** Every other read is genuinely
time-dependent.

## The gate that replaces the audit

An audit is a snapshot; the next measurement site added will not be in this table. So
the property is enforced by a test that discovers its own scope:

`tests/statistical/test_time_invariance.py` runs **every inferential entry point** under
three installed clocks eighteen months apart and requires byte-identical output. New
inferential functions are registered in `INFERENTIAL_ENTRY_POINTS`, and a companion test
asserts that anything in `antar/eval/` returning a number is either registered or
explicitly exempted with a reason.

## The corrected definition of determinism

`tests/statistical/test_determinism.py` checked *seed*-invariance and passed for the
entire life of D13. That definition was incomplete. The full property is:

1. Same seed **and** same pinned instant → identical result. *(was checked)*
2. **Any** instant → identical result, for anything inferential. *(was not checked)*

Both are now gates. This is the same move as the canary defence — generalising from the
specific instance to the class — applied to time instead of data.
