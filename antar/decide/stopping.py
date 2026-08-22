"""Stopping rules. `C-STOP` from PLAN.md §3.3. **No LLM.**

Seven predicates, any one of which ends the conversation with a customer. They are
evaluated *before* the allocator sees a candidate, because a stopping rule is not a
cost to be traded against expected recovery - it is a boundary.

## The one that matters

> **stop when the uplift confidence interval lies entirely below zero** — the
> sleeping-dogs rule.

Note the asymmetry, which is deliberate. Antar does not stop when the *point estimate*
is negative; it stops when the whole interval is. A point estimate of -0.001 with an
interval spanning zero is a customer we know nothing about, and the answer to knowing
nothing is not to act confidently in either direction - it is to fall back to the
budget-constrained allocator and let the shadow price decide.

Conversely a *positive* point estimate with an interval entirely below zero cannot
happen, and if it ever does the model is broken rather than the customer unusual.

## Why these are separate from the regulations

`antar/policy/regulations.py` encodes what the law forbids. This module encodes what
the merchant has decided is not worth doing, plus what the evidence says is actively
harmful. Mixing them would make it impossible to answer "what did compliance cost us?"
- the question `docs/EVALUATION.md` §7.4 exists to answer - because the compliance cost
would be tangled up with policy choices we made freely.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from antar.signals.schemas import CustomerContext, MandateState


class StopReason(StrEnum):
    OPTOUT_RECEIVED = "OPTOUT_RECEIVED"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_PAUSED = "MANDATE_PAUSED"
    ATTEMPT_CAP = "ATTEMPT_CAP"
    CONTACT_BUDGET_EXHAUSTED = "CONTACT_BUDGET_EXHAUSTED"
    NEGATIVE_UPLIFT_CI = "NEGATIVE_UPLIFT_CI"
    NO_UPLIFT_MODEL = "NO_UPLIFT_MODEL"


HUMAN_READABLE: dict[StopReason, str] = {
    StopReason.OPTOUT_RECEIVED: (
        "The customer used the RBI-EM-02 opt-out. Absolute, with no grace window."
    ),
    StopReason.PROMISE_TO_PAY: (
        "A promise to pay is on record. Chasing past it converts a paying customer "
        "into a complaint."
    ),
    StopReason.MANDATE_REVOKED: (
        "The mandate is revoked or completed. There is no consent basis left to "
        "message on (TRAI-04) and nothing to collect."
    ),
    StopReason.MANDATE_PAUSED: (
        "The customer paused the mandate. A paused mandate is not a retry candidate."
    ),
    StopReason.ATTEMPT_CAP: "The per-event attempt cap has been reached.",
    StopReason.CONTACT_BUDGET_EXHAUSTED: (
        "This customer's 30-day contact budget is spent."
    ),
    StopReason.NEGATIVE_UPLIFT_CI: (
        "The estimated uplift confidence interval lies entirely below zero: contacting "
        "this customer is expected to REDUCE recovery, because the RBI-mandated "
        "pre-debit notification is itself a cancellation prompt. The money is made by "
        "staying silent."
    ),
    StopReason.NO_UPLIFT_MODEL: (
        "No uplift model is available. Antar refuses to act rather than falling back "
        "to targeting everyone: the safe failure for a money system is to do nothing."
    ),
}


@dataclass(frozen=True)
class StopDecision:
    stopped: bool
    reason: StopReason | None = None

    @property
    def explanation(self) -> str:
        return HUMAN_READABLE[self.reason] if self.reason else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "reason": self.reason.value if self.reason else None,
            "explanation": self.explanation,
        }


PROCEED = StopDecision(stopped=False)


@dataclass(frozen=True)
class StoppingPolicy:
    max_attempts_per_event: int = 4
    contacts_per_30d: int = 3
    stop_if_ci_upper_below: float = 0.0

    @classmethod
    def from_config(cls, config) -> StoppingPolicy:
        return cls(
            max_attempts_per_event=int(config.get("budgets.max_attempts_per_event")),
            contacts_per_30d=int(config.get("budgets.contacts_per_30d")),
            stop_if_ci_upper_below=float(config.get("decide.stop_if_ci_upper_below")),
        )

    def evaluate(
        self,
        customer: CustomerContext,
        *,
        uplift_ci: tuple[float, float] | None = None,
        has_model: bool = True,
    ) -> StopDecision:
        """First matching rule wins, in descending order of finality.

        Ordering matters for the audit trail rather than for the outcome: a customer
        who has both opted out and exhausted their budget should be recorded as having
        opted out, because that is the fact a human reading the trace needs.
        """
        if not has_model:
            return StopDecision(True, StopReason.NO_UPLIFT_MODEL)
        if customer.optout_received:
            return StopDecision(True, StopReason.OPTOUT_RECEIVED)
        if customer.mandate_state in (MandateState.REVOKED, MandateState.COMPLETED):
            return StopDecision(True, StopReason.MANDATE_REVOKED)
        if customer.mandate_state is MandateState.PAUSED:
            return StopDecision(True, StopReason.MANDATE_PAUSED)
        if customer.promise_to_pay_at is not None:
            return StopDecision(True, StopReason.PROMISE_TO_PAY)
        if customer.attempts_on_event >= self.max_attempts_per_event:
            return StopDecision(True, StopReason.ATTEMPT_CAP)
        if customer.contacts_in_window >= self.contacts_per_30d:
            return StopDecision(True, StopReason.CONTACT_BUDGET_EXHAUSTED)
        if uplift_ci is not None:
            _low, high = uplift_ci
            # The whole interval below zero, not merely the point estimate. An
            # interval straddling zero means we do not know, and "we do not know" is
            # a reason to let the allocator decide under budget - not a reason to
            # abstain confidently.
            if high < self.stop_if_ci_upper_below:
                return StopDecision(True, StopReason.NEGATIVE_UPLIFT_CI)
        return PROCEED


def abstained_value_paise(
    decisions: Iterable[StopDecision],
    amounts: Iterable[int],
    true_uplift: Iterable[float],
    *,
    optout_loss_multiplier: float = 6.0,
) -> int:
    """Rupees earned by *not acting*, for the events where a stopping rule fired.

    PLAN.md M5 asks for this to be quantified separately from rupees earned by acting,
    and the separation is the point: a recovery system that only counts what it did
    cannot see the value of what it declined to do.
    """
    total = 0.0
    for decision, amount, uplift in zip(decisions, amounts, true_uplift, strict=True):
        if not decision.stopped:
            continue
        if uplift < 0:
            total += -uplift * amount * optout_loss_multiplier
        else:
            total -= uplift * amount
    return round(total)


def summarise(decisions: Iterable[StopDecision]) -> dict[str, Any]:
    from collections import Counter

    decisions = list(decisions)
    counts = Counter(d.reason.value for d in decisions if d.reason)
    stopped = sum(1 for d in decisions if d.stopped)
    return {
        "evaluated": len(decisions),
        "stopped": stopped,
        "stop_rate": round(stopped / len(decisions), 5) if decisions else 0.0,
        "by_reason": dict(sorted(counts.items())),
    }


def predicates() -> dict[str, Callable[..., bool]]:
    """The rule set, exposed for the console's 'what stopped us?' panel."""
    return {
        StopReason.OPTOUT_RECEIVED.value: lambda c, **_: c.optout_received,
        StopReason.MANDATE_REVOKED.value: lambda c, **_: c.mandate_state
        in (MandateState.REVOKED, MandateState.COMPLETED),
        StopReason.MANDATE_PAUSED.value: lambda c, **_: c.mandate_state is MandateState.PAUSED,
        StopReason.PROMISE_TO_PAY.value: lambda c, **_: c.promise_to_pay_at is not None,
    }
