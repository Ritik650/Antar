"""The mandate state machine.

**No LLM.**

Five states, explicit legal transitions, and one rule that matters more than the rest:
**a mandate that is not `ACTIVE` is not a retry candidate.** `RBI-EM-06` gives the
customer the right to modify, pause, or withdraw a mandate at any time, so `PAUSED` is
a real state rather than a synonym for broken, and debiting a paused mandate is not a
bug in our retry logic - it is a debit the customer told us not to make.

## Ordering

Transitions are applied in the order events *happened*, not the order they arrived.
Razorpay redelivers, and a `subscription.cancelled` that arrives before the
`payment.failed` it followed would otherwise resurrect a dead mandate. `apply_stream`
sorts by the payload timestamp before folding.

Terminal states absorb. Once `REVOKED`, no event moves it - a late `subscription.
charged` for a cancelled subscription is a delivery artefact, not a resurrection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from antar.signals.schemas import MandateState

# Legal transitions. Anything not listed is illegal and raises rather than being
# silently coerced - a mandate quietly moving from REVOKED to ACTIVE is exactly the
# kind of bug that produces an unauthorised debit.
LEGAL: dict[MandateState, frozenset[MandateState]] = {
    MandateState.ACTIVE: frozenset(
        {
            MandateState.ACTIVE,
            MandateState.PAUSED,
            MandateState.HALTED,
            MandateState.REVOKED,
            MandateState.COMPLETED,
        }
    ),
    # A halted mandate (too many failures) can be revived by a successful charge.
    MandateState.HALTED: frozenset(
        {MandateState.HALTED, MandateState.ACTIVE, MandateState.REVOKED, MandateState.COMPLETED}
    ),
    # RBI-EM-06: pausing is a customer right, and resuming is theirs to exercise.
    MandateState.PAUSED: frozenset(
        {MandateState.PAUSED, MandateState.ACTIVE, MandateState.REVOKED, MandateState.COMPLETED}
    ),
    # Terminal.
    MandateState.REVOKED: frozenset({MandateState.REVOKED}),
    MandateState.COMPLETED: frozenset({MandateState.COMPLETED}),
}

TERMINAL: frozenset[MandateState] = frozenset({MandateState.REVOKED, MandateState.COMPLETED})

# States in which Antar may attempt a debit or a contact about a debit.
RETRYABLE: frozenset[MandateState] = frozenset({MandateState.ACTIVE, MandateState.HALTED})

EVENT_TRANSITIONS: dict[str, MandateState] = {
    "subscription.charged": MandateState.ACTIVE,
    "subscription.resumed": MandateState.ACTIVE,
    "subscription.pending": MandateState.ACTIVE,
    "subscription.halted": MandateState.HALTED,
    "subscription.paused": MandateState.PAUSED,
    "subscription.cancelled": MandateState.REVOKED,
    "subscription.completed": MandateState.COMPLETED,
}


class IllegalTransition(Exception):
    """Raised when a transition is not in the legal set."""

    def __init__(self, subscription_id: str, source: MandateState, target: MandateState) -> None:
        super().__init__(
            f"{subscription_id}: illegal mandate transition {source.value} -> {target.value}"
        )
        self.subscription_id = subscription_id
        self.source = source
        self.target = target


@dataclass
class MandateMachine:
    """One mandate's state and the history that produced it."""

    subscription_id: str
    state: MandateState = MandateState.ACTIVE
    transitions: list[tuple[datetime, MandateState, MandateState, str]] = field(
        default_factory=list
    )
    ignored: list[tuple[datetime, MandateState, str]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def can_retry(self) -> bool:
        """Whether Antar may attempt anything at all against this mandate."""
        return self.state in RETRYABLE

    def apply(self, target: MandateState, *, at: datetime, cause: str = "") -> MandateState:
        """Move to `target`. Terminal states absorb; illegal transitions raise."""
        source = self.state

        if source in TERMINAL:
            # Absorb rather than raise. A late-delivered event for a cancelled
            # subscription is normal operation, not a programming error, and
            # crashing the batch over it would be the wrong response.
            if target is not source:
                self.ignored.append((at, target, cause or "post-terminal event"))
            return self.state

        if target not in LEGAL[source]:
            raise IllegalTransition(self.subscription_id, source, target)

        if target is not source:
            self.transitions.append((at, source, target, cause))
        self.state = target
        return self.state

    def apply_event(self, event_name: str, *, at: datetime) -> MandateState:
        target = EVENT_TRANSITIONS.get(event_name)
        if target is None:
            return self.state
        return self.apply(target, at=at, cause=event_name)

    def why_not_retryable(self) -> str | None:
        """A sentence for the audit trace. `None` when the mandate is retryable."""
        if self.can_retry:
            return None
        return {
            MandateState.PAUSED: (
                "The customer paused this mandate (RBI-EM-06). A paused mandate is "
                "not a retry candidate; debiting it would be a debit they told us "
                "not to make."
            ),
            MandateState.REVOKED: (
                "The mandate has been revoked. Terminal - never contact again about "
                "this subscription."
            ),
            MandateState.COMPLETED: "The subscription ran to term. Nothing is owed.",
        }.get(self.state, f"Mandate is {self.state.value}.")


class MandateRegistry:
    """Every mandate we know about."""

    def __init__(self) -> None:
        self.machines: dict[str, MandateMachine] = {}
        self.illegal_attempts: list[IllegalTransition] = []

    def get(self, subscription_id: str) -> MandateMachine:
        machine = self.machines.get(subscription_id)
        if machine is None:
            machine = MandateMachine(subscription_id=subscription_id)
            self.machines[subscription_id] = machine
        return machine

    def state_of(self, subscription_id: str | None) -> MandateState:
        """A checkout abandonment has no mandate; treat it as active for retry
        purposes, because there is no mandate state standing in the way."""
        if subscription_id is None:
            return MandateState.ACTIVE
        return self.get(subscription_id).state

    def state_at(self, subscription_id: str | None, when: datetime) -> MandateState:
        """The state as of `when`, using only transitions known by then.

        This is the method the diagnoser must use, and the distinction is not
        pedantry. A cancellation webhook is timestamped at the moment the customer
        cancelled - which is the same moment the debit failed. Asking for the
        *current* state while diagnosing a historical failure lets the detector see a
        cancellation it could not have known about, and turns `MANDATE_REVOKED`
        recall into a measurement of hindsight. POSTMORTEM D10.
        """
        if subscription_id is None:
            return MandateState.ACTIVE
        machine = self.get(subscription_id)
        state = MandateState.ACTIVE
        for at, _source, target, _cause in machine.transitions:
            if at >= when:
                break
            state = target
        return state

    def can_retry(self, subscription_id: str | None) -> bool:
        if subscription_id is None:
            return True
        return self.get(subscription_id).can_retry

    def apply_stream(self, events: Iterable[tuple[str, str, datetime]]) -> MandateRegistry:
        """Fold `(subscription_id, event_name, occurred_at)` triples.

        Sorted by `occurred_at` first, so arrival order is irrelevant and the final
        state is a function of what happened rather than of network weather.
        """
        for subscription_id, event_name, at in sorted(events, key=lambda row: (row[2], row[1])):
            try:
                self.get(subscription_id).apply_event(event_name, at=at)
            except IllegalTransition as exc:
                # Recorded rather than raised: one malformed stream should not take
                # down a batch of eight thousand. Surfaced in the console.
                self.illegal_attempts.append(exc)
        return self

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {state.value: 0 for state in MandateState}
        for machine in self.machines.values():
            counts[machine.state.value] += 1
        counts["illegal_transitions_rejected"] = len(self.illegal_attempts)
        return counts
