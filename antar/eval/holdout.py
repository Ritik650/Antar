"""Randomised control assignment.

docs/EVALUATION.md section 3. Three properties, each of which the evaluation is
worthless without:

  * **Randomisation is at the customer, not the event.** Randomising per event would
    leak treatment across cycles for the same person and contaminate the control.
    Assignment is sticky for the whole 120-day horizon.
  * **Assignment is a deterministic hash of `(customer_id, salt)`.** Reproducible
    from the seed, identical across runs and machines, impossible to drift.
  * **A control customer never enters a training set.** `training_filter` is the one
    sanctioned way to build a training index, and
    `tests/statistical/test_control_isolation.py` fails the build if a control
    event id appears in one.

The control arm still receives the RBI-EM-01 pre-transaction notification for its
*scheduled* debits, because that is the issuer's obligation and not Antar's action.
What it does not receive is any additional Antar-initiated retry, message, or
discount. That is the correct counterfactual: it isolates our marginal contribution
rather than the effect of the regulatory baseline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from antar.signals.schemas import Arm, AtRiskEvent

_RESOLUTION = 1_000_000


def _uniform(customer_id: str, salt: str, *, purpose: str = "arm") -> float:
    """A stable uniform draw in [0, 1) from a customer id."""
    digest = hashlib.blake2b(
        f"{salt}\x1f{purpose}\x1f{customer_id}".encode(), digest_size=8
    ).digest()
    return (int.from_bytes(digest, "big") % _RESOLUTION) / _RESOLUTION


@dataclass(frozen=True)
class HoldoutAssignment:
    """Which arm a customer is in, and why it is reproducible."""

    customer_id: str
    arm: Arm
    draw: float
    salt: str

    @property
    def is_control(self) -> bool:
        return self.arm is Arm.CONTROL


class Holdout:
    """Deterministic, sticky, stratification-aware arm assignment."""

    def __init__(self, *, control_share: float = 0.20, salt: str = "antar") -> None:
        if not 0.0 < control_share < 1.0:
            raise ValueError(f"control_share must be in (0, 1), got {control_share}")
        self.control_share = control_share
        self.salt = salt

    def assign(self, customer_id: str) -> HoldoutAssignment:
        draw = _uniform(customer_id, self.salt)
        arm = Arm.CONTROL if draw < self.control_share else Arm.TREATMENT
        return HoldoutAssignment(customer_id=customer_id, arm=arm, draw=draw, salt=self.salt)

    def arm_of(self, customer_id: str) -> Arm:
        return self.assign(customer_id).arm

    def is_control(self, customer_id: str) -> bool:
        return self.arm_of(customer_id) is Arm.CONTROL

    def is_control_event(self, event: AtRiskEvent) -> bool:
        return self.is_control(event.customer_id)

    # ------------------------------------------------------------- splitting

    def split(self, events: Iterable[AtRiskEvent]) -> tuple[list[AtRiskEvent], list[AtRiskEvent]]:
        """(treatment, control), preserving input order."""
        treatment: list[AtRiskEvent] = []
        control: list[AtRiskEvent] = []
        for event in events:
            (control if self.is_control_event(event) else treatment).append(event)
        return treatment, control

    def training_filter(self, events: Iterable[AtRiskEvent]) -> Iterator[AtRiskEvent]:
        """The only sanctioned way to build a training index.

        Anything that trains on the output of this generator cannot see a control
        customer, because control events never come out of it.
        """
        for event in events:
            if not self.is_control_event(event):
                yield event

    def exploration_split(
        self, events: Iterable[AtRiskEvent], *, epsilon_only: bool = False,
        exploration_ids: frozenset[str] | None = None,
    ) -> list[AtRiskEvent]:
        """Model-development data: treatment events, optionally only the
        epsilon-randomised ones.

        docs/EVALUATION.md section 3.4 permits free iteration on this split. The
        control holdout is unblinded exactly once, at M8.
        """
        treatable = list(self.training_filter(events))
        if not epsilon_only:
            return treatable
        if exploration_ids is None:
            raise ValueError("epsilon_only requires the set of exploration event ids")
        return [e for e in treatable if e.event_id in exploration_ids]

    # ------------------------------------------------------------- balance

    def arm_counts(self, customer_ids: Iterable[str]) -> dict[Arm, int]:
        counts = {Arm.CONTROL: 0, Arm.TREATMENT: 0}
        for customer_id in customer_ids:
            counts[self.arm_of(customer_id)] += 1
        return counts

    def realised_control_share(self, customer_ids: Iterable[str]) -> float:
        counts = self.arm_counts(customer_ids)
        total = counts[Arm.CONTROL] + counts[Arm.TREATMENT]
        return 0.0 if total == 0 else counts[Arm.CONTROL] / total


def stratum_of(event: AtRiskEvent, *, afa_ceiling_paise: int) -> str:
    """The stratification key from docs/EVALUATION.md section 3.3.

    Issuer x method, an amount bucket cut at the AFA threshold, and merchant
    category. Balance is checked on this key rather than assumed - a hash-based
    assignment is unbiased in expectation but any single batch can still come out
    lopsided, and the covariate balance table is where that would show.
    """
    bucket = "above_afa" if event.amount_paise > afa_ceiling_paise else "below_afa"
    return f"{event.segment_key}|{bucket}|{event.merchant_category.value}"
