# DECISIONS.md — Architecture Decision Record log

Append-only. Never edit a prior entry; supersede it with a new one and mark the old
one `Superseded by ADR-nnnn`.

Format: **ID · date · title · status** — context, decision, consequence.

---

## ADR-0001 · 2026-08-22 · Why Track 03 · Accepted

**Context.** Four tracks were open. Track 03 (AI Revenue Recovery) asks for "measured
money recovered across a batch, with compliant escalation, stopping rules, and an
audit trail."

**Decision.** Build Track 03, and treat the word *measured* as the load-bearing one.
The obvious build for this track is a dunning agent with a retry schedule. That build
cannot answer the only question that matters — how much of the recovered money would
have arrived anyway — because it has no control group. We build the measurement
apparatus first and the recovery policy second.

**Consequence.** A randomised holdout exists from the first batch, which costs us 20%
of the treatable population and buys the only defensible number in the submission.
Gross recovery is reported alongside incremental recovery everywhere, so the gap is
always visible.

---

## ADR-0002 · 2026-08-22 · Python 3.13 on the build machine, 3.11 as the floor · Accepted

**Context.** PLAN.md section 5 specifies Python 3.11 for the econml/scikit ecosystem.
The build machine has 3.13.2 and no 3.11 interpreter.

**Decision.** Target `requires-python = ">=3.11"`, develop on 3.13, pin CI and the
Docker image to 3.11. econml 0.16, LightGBM 4.6, and PuLP 3.3 all import and run on
3.13; where a library behaves differently, CI on 3.11 is the arbiter.

**Consequence.** `StrEnum` (3.11+) is used freely. Any 3.12+ syntax is banned so the
3.11 floor stays real. PLAN.md section 5 updated in the same commit, per rule 5.

---

## ADR-0003 · 2026-08-22 · `tasks.py` is the build system; the Makefile is an alias · Accepted

**Context.** N4 requires `make evaluate` to reproduce every number from a clean
checkout. The build machine is Windows and has no `make`.

**Decision.** Every target is implemented once, in `tasks.py`, invoked as
`python tasks.py <target>`. The `Makefile` contains no logic; each rule shells out to
`tasks.py`.

**Consequence.** The reproducibility claim does not depend on a build tool being
installed. `make evaluate` and `python tasks.py evaluate` are the same code path, so
they cannot drift apart.

---

## ADR-0004 · 2026-08-22 · IST as a fixed +05:30 offset, not a zoneinfo lookup · Accepted

**Context.** `TRAI-01` confines contact to 09:00–21:00 recipient-local time, and
`RBI-EM-01` imposes a 24-hour lead time. Both need a timezone. `zoneinfo` requires
the `tzdata` package on Windows, adding a dependency to a legally load-bearing path.

**Decision.** Model IST as `timezone(timedelta(hours=5, minutes=30))` in
`antar/clock.py`. India observes no daylight saving and has a single timezone, so a
fixed offset is not an approximation — it is exact.

**Consequence.** No `tzdata` dependency. If Antar is ever extended beyond India this
decision must be revisited, and `CustomerContext.timezone_offset_minutes` is the seam
where that would happen.

---

## ADR-0005 · 2026-08-22 · Deterministic content-derived ids instead of random ULIDs · Accepted

**Context.** PLAN.md section 9.3 requires a fixed seed to produce a byte-identical
event stream and an identical ledger hash chain. Random ULIDs break that immediately.

**Decision.** `antar/ids.py` derives every id as a blake2b digest of its content,
rendered in Crockford base32. Same inputs, same id, on any machine.

**Consequence.** Ids are stable across reruns, which makes the ledger chain hash a
genuine regression signal and makes idempotency keys reconstructible by a restarted
worker rather than needing to be persisted first. The cost is that two genuinely
distinct events with identical content collapse to one id; the id inputs therefore
always include the cycle number and timestamp.

---

## ADR-0006 · 2026-08-22 · SQLite by default, Postgres in Docker · Accepted

**Context.** N4 requires `make evaluate` to run from a clean checkout. Requiring
Docker and a running Postgres to reproduce a number adds a failure mode between the
reviewer and the evidence.

**Decision.** `storage.database_url` defaults to a local SQLite file.
`docker-compose.yml` overrides it with Postgres. The append-only ledger constraint is
enforced by a trigger in both dialects, written once per dialect in
`antar/audit/ledger.py`.

**Consequence.** A reviewer with only Python can reproduce every number. The
production story still uses Postgres, and the ledger's append-only guarantee is
tested against whichever engine is configured.

---

## ADR-0007 · 2026-08-22 · Synthesised fixtures, with a one-command path to real ones · Accepted

**Context.** PLAN.md M1 requires recorded payloads from a live Razorpay test-mode
account, and predicts several test-mode/live divergences to be found in week one. No
test-mode credentials were available for this build.

**Decision.** Synthesise fixtures from the public documentation
(`scripts/make_fixtures.py`), label their provenance loudly in
`tests/fixtures/README.md` and `docs/LIMITATIONS.md` L1, and write
`scripts/record_fixtures.py` so that a single command with real keys replaces them
and prints every structural divergence. `tests/conftest.py` prefers `recorded/` over
`webhooks/`, so the swap needs no test changes.

**Consequence.** The integration layer is structurally tested and not empirically
tested, and the repo says so rather than implying otherwise. The M1 acceptance
criterion "document every test-mode divergence" is recorded as **not measured**, not
as zero. This is the single largest known gap in the build.
