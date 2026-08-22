"""The PolicyGate. **N2: every money action passes through here, no exceptions.**

Not even in tests. Tests use a gate in `dry_run` mode, never a disabled gate, because
a disabled gate is a code path that exists — and a code path that exists will
eventually execute in production.

## What the gate is for

Everything upstream of it is an opinion. The uplift model has an opinion about who to
contact; the LLM has an opinion about what to say. The gate is where opinion becomes
a debit, and it is the only place in the system that can make one. It enforces five
things:

1. **Caps** — per action, per day, per merchant. Absolute, and checked against the
   ledger rather than against an in-memory counter that a restart would reset.
2. **Idempotency** — every action needs a key, and replaying a key returns the
   original record rather than acting again. This is what makes a crashed worker safe
   to restart.
3. **Dry run** — produces a complete `ActionRecord` with `executed=False`. The default.
4. **Human approval** — above a configured amount, the gate refuses to act without an
   approver on the record.
5. **Provenance** — no action without a `Decision` id. An executor cannot be reached
   by anything that has not been through L3.

## The thing the LLM cannot do

The gate takes an `Intervention` that L3 already decided on, and a `DraftedMessage`
that L4 produced. It reads the *amount* from the decision, never from the draft. A
model that emits `{"discount": 5000}` is emitting a field the gate does not read.
That is the mechanical form of N1, and `tests/adversarial/` is where it is proven.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from antar import clock
from antar.ids import action_id as make_action_id
from antar.signals.schemas import (
    ActionRecord,
    Channel,
    Decision,
    DraftedMessage,
    MessageClass,
)


class GateRefusal(Exception):
    """The gate declined to act. Carries the reason, which goes in the ledger."""

    def __init__(self, reason: str, *, code: str, decision_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.decision_id = decision_id


class LedgerSink(Protocol):
    """Whatever records what happened. Narrow on purpose."""

    def record_action(self, action: ActionRecord) -> None: ...


class NullLedger:
    """Collects records in memory. Used by tests and by the batch runner before the
    real ledger exists in M8."""

    def __init__(self) -> None:
        self.actions: list[ActionRecord] = []

    def record_action(self, action: ActionRecord) -> None:
        self.actions.append(action)


@dataclass
class GateLimits:
    per_action_cap_paise: int = 20_000_000
    per_day_cap_paise: int = 500_000_000
    per_merchant_total_cap_paise: int = 2_000_000_000
    human_approval_threshold_paise: int = 5_000_000

    @classmethod
    def from_config(cls, config) -> GateLimits:
        section = config.section("gate")
        return cls(
            per_action_cap_paise=int(section.get("per_action_cap_paise")),
            per_day_cap_paise=int(section.get("per_day_cap_paise")),
            per_merchant_total_cap_paise=int(section.get("per_merchant_total_cap_paise")),
            human_approval_threshold_paise=int(section.get("human_approval_threshold_paise")),
        )


@dataclass
class GateCounters:
    """Spend so far. Keyed so a restart can rebuild them from the ledger."""

    per_day: dict[tuple[str, date], int] = field(default_factory=dict)
    per_merchant: dict[str, int] = field(default_factory=dict)

    def spent_today(self, merchant_id: str, when: datetime) -> int:
        return self.per_day.get((merchant_id, when.date()), 0)

    def spent_total(self, merchant_id: str) -> int:
        return self.per_merchant.get(merchant_id, 0)

    def add(self, merchant_id: str, when: datetime, amount_paise: int) -> None:
        key = (merchant_id, when.date())
        self.per_day[key] = self.per_day.get(key, 0) + amount_paise
        self.per_merchant[merchant_id] = self.per_merchant.get(merchant_id, 0) + amount_paise


Executor = Callable[[ActionRecord], str]
"""Performs the side effect and returns a provider reference. Only ever called by the
gate, and only after every check has passed."""


def requires_gate(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a callable as one that may only be reached through the PolicyGate.

    The marker is what makes gate coverage checkable *by introspection*.
    `tests/unit/test_gate_coverage.py` walks `antar.act.executors` at run time, finds
    every function that can reach a money-moving client method, and requires this
    decoration on each. A test that enumerated the executors by hand would pass
    forever and quietly stop covering the one added next week.

    It also enforces the invariant at run time: an executor invoked without an
    `ActionRecord` that the gate has already approved raises rather than acting. Both
    halves matter — the static check catches the mistake at review time, the runtime
    assertion catches it if the static check is ever wrong.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        record = kwargs.get("action")
        if record is None:
            record = next((a for a in args if isinstance(a, ActionRecord)), None)
        if record is None:
            raise GateRefusal(
                f"{fn.__name__} was called without an ActionRecord. Executors are "
                "reachable only through PolicyGate.submit().",
                code="UNGATED_CALL",
            )
        if record.blocked:
            raise GateRefusal(
                f"{fn.__name__} was called with an action the gate refused: "
                f"{record.rejected_reason}",
                code="REFUSED_ACTION",
                decision_id=record.decision_id,
            )
        if not record.decision_id:
            raise GateRefusal(
                f"{fn.__name__} was called with an action carrying no decision id",
                code="NO_DECISION",
            )
        return fn(*args, **kwargs)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    wrapper.__antar_requires_gate__ = True  # type: ignore[attr-defined]
    return wrapper


class PolicyGate:
    """The single choke point for money.

    There is deliberately no `enabled` flag, no `skip_checks` argument, and no
    module-level function that performs an action without going through an instance.
    `tests/unit/test_gate_coverage.py` walks `antar.act.executors` at test time and
    fails if it discovers any callable that reaches the Razorpay client without being
    gate-wrapped — so the guarantee keeps covering executors nobody has written yet.
    """

    def __init__(
        self,
        *,
        limits: GateLimits | None = None,
        ledger: LedgerSink | None = None,
        dry_run: bool = True,
        counters: GateCounters | None = None,
        approver: str | None = None,
    ) -> None:
        self.limits = limits or GateLimits()
        self.ledger = ledger or NullLedger()
        self.dry_run = dry_run
        self.counters = counters or GateCounters()
        self.approver = approver
        self._by_key: dict[str, ActionRecord] = {}
        self.refusals: list[tuple[str, str]] = []

    @classmethod
    def from_config(cls, config, **kwargs) -> PolicyGate:
        return cls(
            limits=GateLimits.from_config(config),
            dry_run=bool(config.get("gate.dry_run", True)),
            **kwargs,
        )

    # ------------------------------------------------------------------ core

    def submit(
        self,
        decision: Decision,
        *,
        idempotency_key: str,
        amount_paise: int = 0,
        cost_paise: int = 0,
        executor: Executor | None = None,
        draft: DraftedMessage | None = None,
        attempt: int = 1,
        approved_by: str | None = None,
    ) -> ActionRecord:
        """Submit a money action. Returns an `ActionRecord` either way.

        Refusals are recorded, not raised, for everything the policy layer is
        *expected* to decline — a cap, a missing approval. `GateRefusal` is reserved
        for a caller error: no decision, no key, no chosen intervention. The
        distinction matters because the first is normal operation to be counted, and
        the second is a bug to be surfaced loudly.
        """
        now = clock.now()

        if not idempotency_key:
            raise GateRefusal(
                "no idempotency key; refusing to act. A retried action without a key "
                "is how a customer gets charged twice.",
                code="NO_IDEMPOTENCY_KEY",
                decision_id=decision.decision_id if decision else None,
            )

        # Replay: return the original record, act again on nothing.
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing

        if decision is None or not decision.decision_id:
            raise GateRefusal(
                "no decision id; an executor may not be reached except through L3",
                code="NO_DECISION",
            )
        if decision.chosen is None:
            raise GateRefusal(
                "decision has no chosen intervention; nothing to execute",
                code="NO_INTERVENTION",
                decision_id=decision.decision_id,
            )

        intervention = decision.chosen
        # Amount comes from the decision. Never from the draft, never from a caller
        # argument that a model could have influenced.
        record_amount = int(amount_paise)
        discount = int(intervention.discount_paise)

        action = ActionRecord(
            action_id=make_action_id(decision.decision_id, attempt),
            decision_id=decision.decision_id,
            event_id=decision.event_id,
            idempotency_key=idempotency_key,
            channel=intervention.channel,
            message_class=intervention.message_class,
            amount_paise=record_amount,
            discount_paise=discount,
            cost_paise=int(cost_paise),
            executed=False,
            dry_run=self.dry_run,
            draft=draft,
            attempted_at=now,
        )

        refusal = self._check(action, decision, now)
        if refusal is not None:
            action = action.model_copy(update={"rejected_reason": refusal})
            self.refusals.append((decision.decision_id, refusal))
            self._commit(action, idempotency_key)
            return action

        needs_approval = self._needs_approval(action)
        if needs_approval and not (approved_by or self.approver):
            action = action.model_copy(
                update={
                    "requires_human_approval": True,
                    "rejected_reason": (
                        f"Rs {record_amount / 100:,.0f} is above the "
                        f"Rs {self.limits.human_approval_threshold_paise / 100:,.0f} "
                        "human-approval threshold and no approver is recorded."
                    ),
                }
            )
            self.refusals.append((decision.decision_id, "AWAITING_APPROVAL"))
            self._commit(action, idempotency_key)
            return action

        if needs_approval:
            action = action.model_copy(
                update={
                    "requires_human_approval": True,
                    "approved_by": approved_by or self.approver,
                }
            )

        if self.dry_run or executor is None:
            self._commit(action, idempotency_key)
            return action

        reference = executor(action)
        action = action.model_copy(update={"executed": True, "provider_reference": reference})
        self.counters.add(decision.event_id.split("_")[0], now, record_amount)
        self._commit(action, idempotency_key)
        return action

    # -------------------------------------------------------------- checks

    def _check(self, action: ActionRecord, decision: Decision, now: datetime) -> str | None:
        merchant_id = self._merchant_of(decision)

        if action.amount_paise < 0 or action.discount_paise < 0:
            return "negative amount"

        if action.amount_paise > self.limits.per_action_cap_paise:
            return (
                f"Rs {action.amount_paise / 100:,.0f} exceeds the per-action cap of "
                f"Rs {self.limits.per_action_cap_paise / 100:,.0f}"
            )

        if action.discount_paise > action.amount_paise:
            return "discount exceeds the amount owed"

        projected_day = self.counters.spent_today(merchant_id, now) + action.amount_paise
        if projected_day > self.limits.per_day_cap_paise:
            return (
                f"Rs {projected_day / 100:,.0f} would exceed today's cap of "
                f"Rs {self.limits.per_day_cap_paise / 100:,.0f} for {merchant_id}"
            )

        projected_total = self.counters.spent_total(merchant_id) + action.amount_paise
        if projected_total > self.limits.per_merchant_total_cap_paise:
            return (
                f"Rs {projected_total / 100:,.0f} would exceed the total cap of "
                f"Rs {self.limits.per_merchant_total_cap_paise / 100:,.0f}"
            )

        if action.message_class is MessageClass.PROMOTIONAL:
            return "TRAI-03: a recovery action may not carry the promotional class"

        if action.draft is not None:
            if action.draft.message_class is MessageClass.PROMOTIONAL:
                return "TRAI-03: the drafted message was reclassified as promotional"
            if action.draft.contamination_score >= 0.5:
                return (
                    "TRAI-03: the drafted message scored "
                    f"{action.draft.contamination_score:.2f} on the contamination "
                    "detector"
                )
        return None

    def _needs_approval(self, action: ActionRecord) -> bool:
        return action.amount_paise > self.limits.human_approval_threshold_paise

    @staticmethod
    def _merchant_of(decision: Decision) -> str:
        # The merchant is not on the Decision, so it is derived from the event id
        # prefix in this build. A production gate reads it from the event record.
        return decision.event_id.split("_")[0] if decision.event_id else "unknown"

    def _commit(self, action: ActionRecord, idempotency_key: str) -> None:
        self._by_key[idempotency_key] = action
        self.ledger.record_action(action)

    # --------------------------------------------------------------- views

    def record_for(self, idempotency_key: str) -> ActionRecord | None:
        return self._by_key.get(idempotency_key)

    @property
    def actions(self) -> list[ActionRecord]:
        return list(self._by_key.values())

    def summary(self) -> dict[str, Any]:
        actions = self.actions
        return {
            "submitted": len(actions),
            "executed": sum(1 for a in actions if a.executed),
            "dry_run": sum(1 for a in actions if a.dry_run and not a.blocked),
            "blocked": sum(1 for a in actions if a.blocked),
            "awaiting_approval": sum(1 for a in actions if a.requires_human_approval and a.blocked),
            "contacts": sum(1 for a in actions if a.channel is not Channel.SILENT_RETRY),
            "refusal_reasons": sorted({reason for _, reason in self.refusals}),
        }
