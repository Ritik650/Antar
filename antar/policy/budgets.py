"""Contact budget, cooldown, and margin budget.

The scarce resources. `C-BUDGET` in particular is the one whose dual becomes the
headline shadow price — "one additional compliant contact slot is worth Rs X to this
merchant" — so how it is counted matters more than its size.

Counting is per **customer**, not per mandate. A customer with three subscriptions
gets three messages a month, not nine. Antar's simulator gives everyone exactly one
mandate so the distinction never bites here, but the accounting is right for when it
does. `docs/LIMITATIONS.md` L5.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from antar.signals.schemas import CONTACT_CHANNELS, ActionRecord, CustomerContext

ROLLING_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class BudgetPolicy:
    contacts_per_30d: int = 3
    cooldown_hours: int = 72
    max_attempts_per_event: int = 4
    margin_budget_fraction: float = 0.02

    @classmethod
    def from_config(cls, config) -> BudgetPolicy:
        section = config.section("budgets")
        return cls(
            contacts_per_30d=int(section.get("contacts_per_30d")),
            cooldown_hours=int(section.get("cooldown_hours")),
            max_attempts_per_event=int(section.get("max_attempts_per_event")),
            margin_budget_fraction=float(section.get("margin_budget_fraction")),
        )

    def margin_budget_paise(self, at_risk_value_paise: int) -> int:
        """`C-MARGIN`: total discount spend across the batch.

        A fraction of the value at risk rather than a fixed rupee figure, so the
        constraint scales with the batch and the shadow price stays comparable
        across runs of different sizes.
        """
        return int(at_risk_value_paise * self.margin_budget_fraction)


@dataclass
class ContactLedger:
    """Who has been contacted, when. Rebuilt from the audit ledger on restart.

    Held separately from `PolicyGate` because the gate answers "may I do this one
    thing" while this answers "what has this customer already had" — and the second
    question has to survive a process restart to be worth asking.
    """

    policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    history: dict[str, list[datetime]] = field(default_factory=dict)

    def record(self, customer_id: str, at: datetime) -> None:
        self.history.setdefault(customer_id, []).append(at)

    def ingest(self, actions: Iterable[ActionRecord], customer_of: dict[str, str]) -> ContactLedger:
        """Replay executed contacts from the ledger."""
        for action in actions:
            if action.blocked or action.channel not in CONTACT_CHANNELS:
                continue
            customer_id = customer_of.get(action.event_id)
            if customer_id and action.attempted_at is not None:
                self.record(customer_id, action.attempted_at)
        return self

    def contacts_in_window(self, customer_id: str, *, as_of: datetime) -> int:
        cutoff = as_of - ROLLING_WINDOW
        return sum(1 for at in self.history.get(customer_id, ()) if at > cutoff)

    def last_contact(self, customer_id: str) -> datetime | None:
        stamps = self.history.get(customer_id)
        return max(stamps) if stamps else None

    def remaining(self, customer_id: str, *, as_of: datetime) -> int:
        return max(self.policy.contacts_per_30d - self.contacts_in_window(customer_id, as_of=as_of), 0)

    def cooldown_clear_at(self, customer_id: str) -> datetime | None:
        last = self.last_contact(customer_id)
        return None if last is None else last + timedelta(hours=self.policy.cooldown_hours)

    def hydrate(self, customer: CustomerContext, *, as_of: datetime) -> CustomerContext:
        """Return the customer context with budget fields filled in.

        The single place these two fields are populated. Leaving each caller to do it
        is how one of them ends up counting a 30-day window as a calendar month.
        """
        return customer.model_copy(
            update={
                "contacts_in_window": self.contacts_in_window(customer.customer_id, as_of=as_of),
                "last_contact_at": self.last_contact(customer.customer_id),
            }
        )
