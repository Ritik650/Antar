"""Replay a batch deterministically from the ledger.

PLAN.md M8: *"`replay.py`: replay a batch deterministically from the ledger."*

## What replay is for, and what it is not for

It is for answering **"would today's code have done the same thing?"** Feed the recorded
inputs back through the current decision path and compare the decisions to the ones on
record. Agreement means a refactor was behaviour-preserving. Disagreement means either
a bug was fixed or a bug was introduced, and either way the diff names every event where
the two differ.

It is **not** a way to re-run history and get a better answer. The recorded outcome
happened under the recorded action. Replaying with a different action produces a
decision, not an outcome — the counterfactual belongs to `eval/policies.py`, which is
honest about being an estimate. This module never invents an outcome for an action that
was never taken, and `ReplayResult` has no field in which to put one.

## Determinism

Two conditions, both enforced rather than assumed:

  * **The clock is pinned to the recorded time.** Every replayed decision runs under a
    `FrozenClock` set to that event's `decided_at`. D13 is the reason this is not
    optional: a claim scan that read `clock.now()` moved a headline number by half a
    percentage point across a midnight, and replay reads the clock far more than that
    scan did.
  * **Nothing executes.** The replay decision function is called without a gate and
    without a client. A replay that could send an SMS is not a replay.

`verify_first=True` (the default) refuses to replay a ledger that does not verify.
Replaying a tampered ledger produces a confident comparison against fiction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from antar import clock
from antar.audit.ledger import Ledger
from antar.audit.trace import Trace, TraceIndex
from antar.signals.schemas import Decision

DecideFn = Callable[[dict[str, Any], dict[str, Any] | None], Decision | None]
"""`(event_payload, diagnosis_payload) -> Decision | None`. Whatever the caller wants
compared: today's L3, an older L3, or a stub."""


class LedgerNotVerified(RuntimeError):
    """Refusing to replay a ledger that does not verify."""


@dataclass(frozen=True)
class Divergence:
    event_id: str
    field: str
    recorded: Any
    replayed: Any

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.event_id}.{self.field}: recorded {self.recorded!r} vs replayed {self.replayed!r}"


@dataclass
class ReplayResult:
    events_replayed: int = 0
    identical: int = 0
    diverged: int = 0
    skipped: int = 0
    divergences: list[Divergence] = field(default_factory=list)
    skipped_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when every replayed event reproduced its recorded decision."""
        return self.diverged == 0

    @property
    def agreement(self) -> float:
        replayed = self.identical + self.diverged
        return self.identical / replayed if replayed else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events_replayed": self.events_replayed,
            "identical": self.identical,
            "diverged": self.diverged,
            "skipped": self.skipped,
            "agreement": round(self.agreement, 4),
            "divergences": [
                {
                    "event_id": d.event_id,
                    "field": d.field,
                    "recorded": d.recorded,
                    "replayed": d.replayed,
                }
                for d in self.divergences
            ],
            "skipped_reasons": self.skipped_reasons,
        }


# The fields whose agreement means "the same decision". Deliberately *not* every field
# on `Decision`: `decision_id` is derived from the versions (ADR-0005), so it differs
# whenever a model version changes even if every substantive choice is identical, and
# comparing it would report a divergence on every version bump and hide the real ones.
COMPARED_FIELDS: tuple[str, ...] = (
    "chosen_channel",
    "chosen_scheduled_for",
    "chosen_discount_paise",
    "is_control",
    "binding_constraints",
    "stopping_rule_fired",
)

# Floats are compared with a tolerance because the point of replay is behavioural
# agreement, not bit-identity: a numpy version that changes the last ulp of an uplift
# estimate has not changed what Antar did.
NUMERIC_FIELDS: tuple[tuple[str, float], ...] = (
    ("uplift_estimate", 1e-9),
    ("propensity", 1e-12),
)


def _comparable(decision: Decision | dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a decision to the fields replay compares."""
    if decision is None:
        return dict.fromkeys(COMPARED_FIELDS) | dict.fromkeys(f for f, _ in NUMERIC_FIELDS)

    payload = decision.model_dump(mode="json") if isinstance(decision, Decision) else decision

    chosen = payload.get("chosen") or {}
    return {
        "chosen_channel": chosen.get("channel"),
        "chosen_scheduled_for": chosen.get("scheduled_for"),
        "chosen_discount_paise": chosen.get("discount_paise", 0) if chosen else None,
        "is_control": payload.get("is_control", False),
        "binding_constraints": sorted(payload.get("binding_constraints") or []),
        "stopping_rule_fired": payload.get("stopping_rule_fired"),
        "uplift_estimate": float(payload.get("uplift_estimate", 0.0)),
        "propensity": float(payload.get("propensity", 1.0)),
    }


def _numerically_differs(recorded: Any, replayed: Any, tolerance: float) -> bool:
    """Tolerant float comparison that copes with one side being absent.

    A replay that returns `None` where the record has a decision has no estimate at
    all, which is a difference in kind rather than in magnitude - so `None` versus a
    number always differs, and `None` versus `None` never does. Coercing `None` to 0.0
    would silently agree with a recorded estimate of exactly zero.
    """
    if recorded is None or replayed is None:
        return recorded is not replayed
    return abs(float(recorded) - float(replayed)) > tolerance


def _recorded_time(trace: Trace) -> datetime | None:
    decision = trace.decision or {}
    stamp = decision.get("decided_at")
    if stamp:
        return datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    for entry in trace.entries:
        return entry.written_at
    return None


def replay(
    ledger: Ledger,
    decide: DecideFn,
    *,
    event_ids: Sequence[str] | None = None,
    verify_first: bool = True,
) -> ReplayResult:
    """Re-decide each recorded event and compare against what was recorded.

    `decide` receives the recorded event and diagnosis payloads and returns a
    `Decision` or `None`. It is not given a gate, a client, or a live clock.
    """
    if verify_first:
        verification = ledger.verify_chain()
        if not verification.ok:
            raise LedgerNotVerified(
                f"the ledger has {len(verification.breaks)} chain break(s), first at "
                f"seq {verification.first_break.seq}. Replaying it would produce a "
                "confident comparison against a record that has been altered. Fix or "
                "quarantine the ledger first, or pass verify_first=False if you are "
                "deliberately inspecting the damage."
            )

    index = TraceIndex(ledger)
    targets = list(event_ids) if event_ids is not None else index.event_ids()
    result = ReplayResult()

    for event_id in targets:
        trace = index_trace(ledger, event_id)

        if trace.event is None:
            result.skipped += 1
            result.skipped_reasons[event_id] = (
                "no EVENT entry: there is nothing to feed back through L3"
            )
            continue
        if trace.decision is None:
            result.skipped += 1
            result.skipped_reasons[event_id] = (
                "no DECISION entry: nothing recorded to compare a replay against"
            )
            continue

        at = _recorded_time(trace)
        if at is None:
            result.skipped += 1
            result.skipped_reasons[event_id] = "no timestamp: the clock cannot be pinned"
            continue

        # Pinned to the recorded moment. D13's lesson, applied where it bites hardest.
        with clock.use_clock(clock.FrozenClock(at)):
            replayed = decide(trace.event, trace.diagnosis)

        result.events_replayed += 1
        recorded_fields = _comparable(trace.decision)
        replayed_fields = _comparable(replayed)

        diffs = [
            Divergence(event_id, name, recorded_fields[name], replayed_fields[name])
            for name in COMPARED_FIELDS
            if recorded_fields[name] != replayed_fields[name]
        ]
        diffs += [
            Divergence(event_id, name, recorded_fields[name], replayed_fields[name])
            for name, tolerance in NUMERIC_FIELDS
            if _numerically_differs(
                recorded_fields[name], replayed_fields[name], tolerance
            )
        ]

        if diffs:
            result.diverged += 1
            result.divergences.extend(diffs)
        else:
            result.identical += 1

    return result


def index_trace(ledger: Ledger, event_id: str) -> Trace:
    from antar.audit.trace import build_trace

    return build_trace(ledger, event_id)


def replay_is_self_consistent(ledger: Ledger) -> ReplayResult:
    """The null replay: feed each recorded decision back as its own answer.

    Must be perfect agreement by construction, which is exactly why it is worth
    running — it tests the comparison machinery rather than the decision path. A
    comparator that reports divergence here is broken, and a comparator that reports
    agreement on *everything* is broken in the other direction (the tests cover both).
    """
    index = TraceIndex(ledger)

    def echo(event: dict[str, Any], _diagnosis: dict[str, Any] | None) -> dict[str, Any] | None:
        trace = index_trace(ledger, str(event.get("event_id")))
        return trace.decision

    return replay(ledger, echo, event_ids=index.event_ids())  # type: ignore[arg-type]


__all__ = [
    "COMPARED_FIELDS",
    "Divergence",
    "LedgerNotVerified",
    "ReplayResult",
    "replay",
    "replay_is_self_consistent",
]
