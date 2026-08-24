"""Compensating transactions for a workflow that fails partway through.

A recovery workflow is several side effects in sequence: create a payment link, send a
message about it, record the contact against the budget. Any of them can fail, and the
ones before it have already happened.

The failure this exists to prevent is the specific one PLAN.md section 10 names —
*"Worker killed mid-saga → compensating transaction runs on restart; no orphaned
charge"*. An abandoned workflow that leaves a live payment link behind means a customer
can pay against a decision Antar withdrew: money arriving with no matching record,
which is a week of reconciliation for whoever finds it.

## Why compensation rather than a transaction

There is no transaction to have. Razorpay does not roll back, and neither does an SMS.
So each step declares how to undo itself, the log of completed steps is durable, and
`compensate()` walks it backwards.

## What cannot be undone

**A delivered message.** Compensation for a send is an apology, not a reversal, and
Antar does not send one automatically: an unprompted "please ignore our last message"
is a second unsolicited contact, spends a second `C-BUDGET` slot, and delivers a second
RBI-EM-02 opt-out prompt. The step records itself as irreversible and the operator
decides. Pretending otherwise would be worse than admitting it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from antar import clock


class StepState(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"
    IRREVERSIBLE = "IRREVERSIBLE"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


@dataclass
class Step:
    """One side effect, and how to undo it.

    `compensate=None` means the step cannot be undone. That is a declaration, not an
    oversight, and `compensate_all` reports it rather than skipping quietly.
    """

    name: str
    forward: Callable[[], Any]
    compensate: Callable[[Any], Any] | None = None
    irreversible_reason: str = ""

    state: StepState = StepState.PENDING
    result: Any = None
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "result": str(self.result) if self.result is not None else None,
            "error": self.error,
            "irreversible_reason": self.irreversible_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass
class SagaResult:
    completed: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    irreversible: list[str] = field(default_factory=list)
    failed_step: str = ""
    error: str = ""
    compensation_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_step

    @property
    def clean(self) -> bool:
        """Fully unwound: nothing failed to compensate and nothing was left behind."""
        return not self.compensation_errors and not self.irreversible

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "clean": self.clean,
            "completed": self.completed,
            "compensated": self.compensated,
            "irreversible": self.irreversible,
            "failed_step": self.failed_step,
            "error": self.error,
            "compensation_errors": self.compensation_errors,
        }


class Saga:
    """Run steps forward; on failure, unwind the completed ones in reverse.

    The step log is the durable part. A worker that dies mid-saga is recovered by
    reloading the log and calling `compensate_all()`, which is why every step records
    its result: compensation needs the id of the thing it is undoing.
    """

    def __init__(self, name: str, *, steps: list[Step] | None = None) -> None:
        self.name = name
        self.steps: list[Step] = steps or []

    def add(
        self,
        name: str,
        forward: Callable[[], Any],
        compensate: Callable[[Any], Any] | None = None,
        *,
        irreversible_reason: str = "",
    ) -> Saga:
        if compensate is None and not irreversible_reason:
            raise ValueError(
                f"step {name!r} has no compensation and no stated reason. A step that "
                "cannot be undone must say why, so that an operator reading the trace "
                "knows it was a decision rather than an omission."
            )
        self.steps.append(
            Step(
                name=name,
                forward=forward,
                compensate=compensate,
                irreversible_reason=irreversible_reason,
            )
        )
        return self

    # ------------------------------------------------------------------ run

    def run(self) -> SagaResult:
        result = SagaResult()
        for step in self.steps:
            step.started_at = clock.now()
            try:
                step.result = step.forward()
            except Exception as exc:
                step.state = StepState.FAILED
                step.error = f"{type(exc).__name__}: {exc}"
                step.finished_at = clock.now()
                result.failed_step = step.name
                result.error = step.error
                self._unwind(result)
                return result

            step.state = StepState.DONE
            step.finished_at = clock.now()
            result.completed.append(step.name)
        return result

    def compensate_all(self) -> SagaResult:
        """Unwind everything already done. The restart path.

        Idempotent: a step already `COMPENSATED` is skipped, so running this twice
        after two crashes does not double-refund.
        """
        result = SagaResult(completed=[s.name for s in self.steps if s.state is StepState.DONE])
        self._unwind(result)
        return result

    def _unwind(self, result: SagaResult) -> None:
        for step in reversed(self.steps):
            if step.state is not StepState.DONE:
                continue

            if step.compensate is None:
                step.state = StepState.IRREVERSIBLE
                result.irreversible.append(step.name)
                continue

            try:
                step.compensate(step.result)
            except Exception as exc:
                # One failed compensation must not strand the others. A live payment
                # link left behind because a refund failed is two problems, not one.
                step.state = StepState.COMPENSATION_FAILED
                message = f"{step.name}: {type(exc).__name__}: {exc}"
                step.error = message
                result.compensation_errors.append(message)
                continue

            step.state = StepState.COMPENSATED
            result.compensated.append(step.name)

    # ---------------------------------------------------------------- views

    def trace(self) -> list[dict[str, Any]]:
        return [step.as_dict() for step in self.steps]

    @property
    def pending(self) -> list[Step]:
        return [s for s in self.steps if s.state is StepState.PENDING]


IRREVERSIBLE_SEND = (
    "A delivered message cannot be recalled. Antar does not auto-send a correction: "
    "that would be a second unsolicited contact, a second C-BUDGET slot, and a second "
    "RBI-EM-02 opt-out prompt. An operator decides."
)


__all__ = ["IRREVERSIBLE_SEND", "Saga", "SagaResult", "Step", "StepState"]
