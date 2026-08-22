"""Compile regulations into LP constraint rows.

`Regulation[] x Candidate[] -> ConstraintRow[]`. A pure function, heavily property
tested, and the only place that decides what the optimiser is allowed to do.

## Two kinds of constraint, and why the distinction matters

**Per-candidate feasibility.** Most regulations are predicates on a single candidate
action: is it 24 hours out, is it inside the contact window, is consent live. These
never become LP rows at all - an infeasible candidate is simply removed from the
problem before the solver sees it. Encoding "this action is illegal" as a constraint
row would let the solver trade it off against the objective, and a regulation is not
something you trade off.

**Cross-candidate capacity.** A few are genuinely shared resources: three contacts per
customer per 30 days, a margin budget across the batch. These cannot be decided one
candidate at a time, so they become real LP rows - and their duals are the shadow
prices the console reports.

The separation is deliberate. Anything a solver could be tempted to violate for
sufficient objective gain is removed from its reach entirely.

## The second validator

`antar/policy/validator.py` re-checks every solution independently, by a dumb, slow,
obviously-correct route. It exists because the interesting bugs in this file are
off-by-one errors on a threshold - a 23-hour lead time that passes because a
comparison used `>` where the regulation says "at least" - and those survive unit
tests written by the same person who wrote the bug. Two implementations disagreeing is
how they get found.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from antar.policy import regulations as reg
from antar.policy.regulations import Regulation, Severity
from antar.signals.schemas import CONTACT_CHANNELS, DecisionContext


class RowKind(StrEnum):
    CONTACT_BUDGET = "CONTACT_BUDGET"
    """<= k contacts per customer per rolling 30 days (C-BUDGET)."""

    MARGIN_BUDGET = "MARGIN_BUDGET"
    """Total discount value across the batch <= merchant margin budget (C-MARGIN)."""

    ONE_ACTION_PER_EVENT = "ONE_ACTION_PER_EVENT"
    """At most one intervention per at-risk event. Structural, not regulatory."""

    CUSTOMER_COOLDOWN = "CUSTOMER_COOLDOWN"
    """At most one contact per customer per cooldown window (C-COOLDOWN)."""


@dataclass(frozen=True)
class ConstraintRow:
    """One LP row: `sum(coefficient_i * x_i) <= bound`.

    `x_i` are binary selection variables indexed by candidate id. The dual of a
    binding row is what one more unit of that resource would be worth, which is the
    number the console reports as a shadow price.
    """

    row_id: str
    kind: RowKind
    coefficients: dict[str, float]
    bound: float
    explanation: str
    regulation_ids: tuple[str, ...] = ()

    def slack(self, selection: dict[str, float]) -> float:
        used = sum(coefficient * selection.get(cid, 0.0) for cid, coefficient in self.coefficients.items())
        return self.bound - used

    def satisfied_by(self, selection: dict[str, float], *, tolerance: float = 1e-9) -> bool:
        return self.slack(selection) >= -tolerance


@dataclass(frozen=True)
class Candidate:
    """One (event, intervention) pair the allocator may choose."""

    candidate_id: str
    context: DecisionContext

    @property
    def event_id(self) -> str:
        return self.context.event.event_id

    @property
    def customer_id(self) -> str:
        return self.context.customer.customer_id

    @property
    def is_contact(self) -> bool:
        return self.context.intervention.channel in CONTACT_CHANNELS

    @property
    def discount_paise(self) -> int:
        return self.context.intervention.discount_paise


@dataclass(frozen=True)
class Infeasibility:
    """Why one candidate was removed before the solver ran."""

    candidate_id: str
    event_id: str
    regulation_ids: tuple[str, ...]
    explanation: str


@dataclass
class CompiledProblem:
    """Everything the allocator needs, and nothing it should not have."""

    feasible: list[Candidate] = field(default_factory=list)
    rejected: list[Infeasibility] = field(default_factory=list)
    rows: list[ConstraintRow] = field(default_factory=list)
    advisory_flags: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def feasible_ids(self) -> set[str]:
        return {c.candidate_id for c in self.feasible}

    def binding_regulations(self) -> set[str]:
        out: set[str] = set()
        for rejection in self.rejected:
            out.update(rejection.regulation_ids)
        for row in self.rows:
            out.update(row.regulation_ids)
        return out

    def summary(self) -> dict[str, Any]:
        from collections import Counter

        blocked = Counter(rid for r in self.rejected for rid in r.regulation_ids)
        return {
            "candidates_in": len(self.feasible) + len(self.rejected),
            "feasible": len(self.feasible),
            "rejected": len(self.rejected),
            "rows": len(self.rows),
            "rejections_by_regulation": dict(sorted(blocked.items())),
            "advisory_flags": {
                rid: sum(1 for flags in self.advisory_flags.values() if rid in flags)
                for rid in sorted({r for flags in self.advisory_flags.values() for r in flags})
            },
        }


def compile_problem(
    candidates: Sequence[Candidate],
    *,
    rules: Iterable[Regulation] = reg.REGULATIONS,
    contacts_per_30d: int = 3,
    margin_budget_paise: int | None = None,
    enforce_cooldown: bool = True,
) -> CompiledProblem:
    """Turn candidates plus regulations into a feasible set and a row list.

    Pure: no clock, no config, no I/O. Everything time-dependent arrives inside each
    candidate's `DecisionContext`, which is what makes the property tests able to
    generate contexts at random and the whole thing reproducible.
    """
    rules = tuple(rules)
    blocking = tuple(r for r in rules if r.severity is Severity.BLOCKING)
    advisory = tuple(r for r in rules if r.severity is Severity.ADVISORY)

    problem = CompiledProblem()

    for candidate in candidates:
        violated = tuple(r.id for r in blocking if r.violated_by(candidate.context))
        if violated:
            problem.rejected.append(
                Infeasibility(
                    candidate_id=candidate.candidate_id,
                    event_id=candidate.event_id,
                    regulation_ids=violated,
                    explanation="; ".join(reg.get(rid).human_explanation for rid in violated),
                )
            )
            continue

        problem.feasible.append(candidate)
        flags = tuple(r.id for r in advisory if r.violated_by(candidate.context))
        if flags:
            problem.advisory_flags[candidate.candidate_id] = flags

    problem.rows = _build_rows(
        problem.feasible,
        contacts_per_30d=contacts_per_30d,
        margin_budget_paise=margin_budget_paise,
        enforce_cooldown=enforce_cooldown,
    )
    return problem


def _build_rows(
    feasible: Sequence[Candidate],
    *,
    contacts_per_30d: int,
    margin_budget_paise: int | None,
    enforce_cooldown: bool,
) -> list[ConstraintRow]:
    rows: list[ConstraintRow] = []

    # --- one action per event (structural) ---------------------------------
    by_event: dict[str, list[Candidate]] = {}
    for candidate in feasible:
        by_event.setdefault(candidate.event_id, []).append(candidate)
    for event_id, group in sorted(by_event.items()):
        if len(group) > 1:
            rows.append(
                ConstraintRow(
                    row_id=f"one_action::{event_id}",
                    kind=RowKind.ONE_ACTION_PER_EVENT,
                    coefficients={c.candidate_id: 1.0 for c in group},
                    bound=1.0,
                    explanation=(
                        "At most one intervention per at-risk cycle. Structural rather "
                        "than regulatory: two simultaneous actions on one cycle would "
                        "make the uplift estimate meaningless."
                    ),
                )
            )

    # --- contact budget per customer (C-BUDGET) ----------------------------
    by_customer: dict[str, list[Candidate]] = {}
    for candidate in feasible:
        if candidate.is_contact:
            by_customer.setdefault(candidate.customer_id, []).append(candidate)

    for customer_id, group in sorted(by_customer.items()):
        already_used = group[0].context.customer.contacts_in_window
        remaining = max(contacts_per_30d - already_used, 0)
        rows.append(
            ConstraintRow(
                row_id=f"contact_budget::{customer_id}",
                kind=RowKind.CONTACT_BUDGET,
                coefficients={c.candidate_id: 1.0 for c in group},
                bound=float(remaining),
                explanation=(
                    f"At most {contacts_per_30d} contacts per customer per rolling 30 "
                    f"days; {already_used} already spent, {remaining} remaining. The "
                    "dual of this row is what one more compliant contact slot is worth."
                ),
                regulation_ids=("POL-BUDGET",),
            )
        )

        if enforce_cooldown and len(group) > 1:
            # Within one batch, the cooldown collapses to "at most one contact per
            # customer". Modelling the full pairwise-gap constraint would need a
            # time-indexed formulation; the scheduler enforces the gap exactly on the
            # chosen action, so this row is the LP-side approximation and is labelled
            # as one rather than presented as exact.
            rows.append(
                ConstraintRow(
                    row_id=f"cooldown::{customer_id}",
                    kind=RowKind.CUSTOMER_COOLDOWN,
                    coefficients={c.candidate_id: 1.0 for c in group},
                    bound=1.0,
                    explanation=(
                        "At most one contact per customer per batch, as the LP-side "
                        "approximation of the cooldown. The scheduler enforces the "
                        "exact gap on whichever action is selected."
                    ),
                    regulation_ids=("POL-COOLDOWN",),
                )
            )

    # --- margin budget across the batch (C-MARGIN) -------------------------
    if margin_budget_paise is not None:
        discounted = {c.candidate_id: float(c.discount_paise) for c in feasible if c.discount_paise}
        if discounted:
            rows.append(
                ConstraintRow(
                    row_id="margin_budget",
                    kind=RowKind.MARGIN_BUDGET,
                    coefficients=discounted,
                    bound=float(margin_budget_paise),
                    explanation=(
                        "Total discount value issued across the batch may not exceed "
                        "the merchant's margin budget. Its dual prices a rupee of "
                        "margin against a rupee of recovery."
                    ),
                    regulation_ids=("POL-MARGIN",),
                )
            )

    return rows


def explain_rejection(problem: CompiledProblem, candidate_id: str) -> str:
    """Plain-language answer to 'why was this action not available?'."""
    for rejection in problem.rejected:
        if rejection.candidate_id == candidate_id:
            names = ", ".join(rejection.regulation_ids)
            return f"Blocked by {names}. {rejection.explanation}"
    if candidate_id in problem.feasible_ids:
        return "Feasible: no blocking regulation applies."
    return f"Unknown candidate {candidate_id!r}."
