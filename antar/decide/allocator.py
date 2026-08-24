"""The LP allocator, and the shadow prices that fall out of it. **No LLM.**

PLAN.md M6. Objective: expected incremental rupees, net of channel cost, discount
value, and expected opt-out loss. Constraints: the rows the policy compiler produced.
Solve with CBC, extract duals, and check the answer against a validator that shares no
code with any of it.

## Why an LP and not a greedy heuristic

The greedy answer — sort by expected value, take until the budget runs out — is
correct when there is exactly one constraint. Antar has several that interact: a
per-customer contact budget, a batch-level capacity, a margin budget, and at most one
action per cycle. Greedy has no way to decline a high-value action because it consumes
a slot two other actions would have used better.

But the real reason is the **duals**. A greedy selection tells the merchant what to do.
An LP tells them what the constraint is *worth* — and "one more compliant contact slot
is worth ₹X" is a sentence a merchant can act on, whereas a list of customer ids is not.

## Two kinds of price, and they are not the same thing

Reported separately, because conflating them would be a quiet overclaim:

  * **LP duals** on capacity rows — contact capacity, margin budget. These are exact
    marginal values from the solved relaxation.
  * **Counterfactual re-solve prices** for constraints that are *feasibility filters*
    rather than capacity rows. `TRAI-01`'s contact window removes candidates before
    the solver ever sees them, so it has no dual. Its price is obtained by re-solving
    with the window widened and differencing the objective.

`ShadowPrice.method` records which of the two produced each number.

## Framing

`docs/EVALUATION.md` §7.4: these are the **price of rules that exist to protect
consumers**, not an argument against them. A merchant learning that the pre-dawn hour
they cannot use is worth ₹X is learning the cost of a protection, and that is a fact
about their operation rather than a grievance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from antar.policy.compiler import Candidate, CompiledProblem, ConstraintRow, RowKind

# Solver status strings we treat as usable.
OPTIMAL = "Optimal"


@dataclass(frozen=True)
class CandidateValue:
    """What one candidate is worth, decomposed so the console can show the arithmetic."""

    candidate_id: str
    expected_incremental_paise: float
    channel_cost_paise: float
    discount_paise: float
    expected_optout_loss_paise: float

    @property
    def net_paise(self) -> float:
        return (
            self.expected_incremental_paise
            - self.channel_cost_paise
            - self.discount_paise
            - self.expected_optout_loss_paise
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "expected_incremental_paise": round(self.expected_incremental_paise, 2),
            "channel_cost_paise": round(self.channel_cost_paise, 2),
            "discount_paise": round(self.discount_paise, 2),
            "expected_optout_loss_paise": round(self.expected_optout_loss_paise, 2),
            "net_paise": round(self.net_paise, 2),
        }


@dataclass(frozen=True)
class ShadowPrice:
    """What one more unit of a constraint would be worth."""

    row_id: str
    kind: str
    price_paise: float
    binding: bool
    method: str
    """`lp_dual` or `counterfactual_resolve`. Not interchangeable, and saying which
    is the difference between a number and a claim."""

    explanation: str
    regulation_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "kind": self.kind,
            "price_paise": round(self.price_paise, 2),
            "price_rupees": round(self.price_paise / 100, 2),
            "binding": self.binding,
            "method": self.method,
            "explanation": self.explanation,
            "regulation_ids": list(self.regulation_ids),
        }


@dataclass
class Allocation:
    selected: list[str] = field(default_factory=list)
    objective_paise: float = 0.0
    status: str = ""
    shadow_prices: list[ShadowPrice] = field(default_factory=list)
    solver: str = "CBC"
    fallback_used: bool = False
    fallback_reason: str = ""
    infeasible_rows: list[str] = field(default_factory=list)
    solve_seconds: float = 0.0

    @property
    def selected_set(self) -> set[str]:
        return set(self.selected)

    def price_for(self, row_id: str) -> ShadowPrice | None:
        return next((p for p in self.shadow_prices if p.row_id == row_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "solver": self.solver,
            "n_selected": len(self.selected),
            "objective_paise": round(self.objective_paise, 2),
            "objective_rupees": round(self.objective_paise / 100, 2),
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "infeasible_rows": self.infeasible_rows,
            "solve_seconds": round(self.solve_seconds, 3),
            "shadow_prices": [p.as_dict() for p in self.shadow_prices],
        }


def contact_capacity_row(
    candidates: list[Candidate], capacity: int, *, row_id: str = "contact_capacity"
) -> ConstraintRow:
    """The batch-level outbound capacity row. **The headline shadow price.**

    A merchant cannot message everyone even when every individual message would be
    compliant: DLT throughput, support headroom, and the appetite to be seen sending
    are all finite. Without a binding capacity there is no scarcity, and without
    scarcity every dual is zero and the allocator is decoration.
    """
    return ConstraintRow(
        row_id=row_id,
        kind=RowKind.CONTACT_BUDGET,
        coefficients={c.candidate_id: 1.0 for c in candidates if c.is_contact},
        bound=float(capacity),
        explanation=(
            f"At most {capacity} outbound contacts across this batch. The dual of this "
            "row is what one additional compliant contact slot is worth to the "
            "merchant."
        ),
        regulation_ids=("POL-CAPACITY",),
    )


def solve(
    problem: CompiledProblem,
    values: dict[str, CandidateValue],
    *,
    extra_rows: list[ConstraintRow] | None = None,
    time_limit_seconds: int = 60,
    greedy_fallback: bool = True,
) -> Allocation:
    """Maximise net incremental rupees subject to the compiled rows.

    Solves twice on purpose: the integer program gives the selection a merchant would
    act on, and the LP relaxation gives the duals. An integer program has no duals, and
    quoting a shadow price from a rounded solution would be making one up.
    """
    import time

    import pulp

    rows = list(problem.rows) + list(extra_rows or [])
    candidates = list(problem.feasible)
    ids = [c.candidate_id for c in candidates]
    if not ids:
        return Allocation(status="Empty", objective_paise=0.0)

    started = time.perf_counter()

    def build(binary: bool):
        model = pulp.LpProblem("antar_allocation", pulp.LpMaximize)
        category = "Binary" if binary else "Continuous"
        variables = {
            cid: pulp.LpVariable(f"x_{index}", lowBound=0, upBound=1, cat=category)
            for index, cid in enumerate(ids)
        }
        model += pulp.lpSum(
            values[cid].net_paise * variables[cid] for cid in ids if cid in values
        )
        for row in rows:
            terms = [
                coefficient * variables[cid]
                for cid, coefficient in row.coefficients.items()
                if cid in variables
            ]
            if terms:
                model += (pulp.lpSum(terms) <= row.bound, row.row_id)
        return model, variables

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_seconds)

    integer_model, integer_vars = build(binary=True)
    integer_status = pulp.LpStatus[integer_model.solve(solver)]

    if integer_status != OPTIMAL:
        if not greedy_fallback:
            return Allocation(
                status=integer_status,
                infeasible_rows=[r.row_id for r in rows],
                solve_seconds=time.perf_counter() - started,
            )
        allocation = _greedy(candidates, values, rows)
        allocation.status = integer_status
        allocation.fallback_used = True
        allocation.fallback_reason = (
            f"CBC returned {integer_status!r}. Fell back to the documented greedy "
            "policy (PLAN.md section 10) rather than acting on an unsolved problem."
        )
        allocation.infeasible_rows = _offending_rows(rows)
        allocation.solve_seconds = time.perf_counter() - started
        return allocation

    selected = [cid for cid in ids if (integer_vars[cid].value() or 0) > 0.5]
    objective = float(pulp.value(integer_model.objective) or 0.0)

    # The relaxation, purely for duals.
    relaxed_model, _ = build(binary=False)
    pulp.LpStatus[relaxed_model.solve(solver)]
    prices = _extract_duals(relaxed_model, rows)

    return Allocation(
        selected=selected,
        objective_paise=objective,
        status=integer_status,
        shadow_prices=prices,
        solve_seconds=time.perf_counter() - started,
    )


def _extract_duals(model: Any, rows: list[ConstraintRow]) -> list[ShadowPrice]:
    prices: list[ShadowPrice] = []
    by_id = {row.row_id: row for row in rows}
    for name, constraint in model.constraints.items():
        row = by_id.get(name)
        if row is None:
            continue
        dual = float(constraint.pi or 0.0)
        slack = float(constraint.slack or 0.0)
        prices.append(
            ShadowPrice(
                row_id=name,
                kind=row.kind.value,
                # PuLP reports duals for a maximisation with the opposite sign
                # convention to the one a merchant expects; a binding capacity should
                # have a positive value.
                price_paise=abs(dual),
                binding=abs(slack) < 1e-6,
                method="lp_dual",
                explanation=row.explanation,
                regulation_ids=row.regulation_ids,
            )
        )
    return sorted(prices, key=lambda p: -p.price_paise)


def _greedy(
    candidates: list[Candidate],
    values: dict[str, CandidateValue],
    rows: list[ConstraintRow],
) -> Allocation:
    """The documented fallback. PLAN.md section 10: an infeasible LP must degrade to a
    stated policy, not to whatever the solver last held.

    Take candidates in descending net value, skipping any that would break a row.
    Deliberately simple, deliberately worse than the LP, and deliberately auditable.
    """
    ordered = sorted(
        (c for c in candidates if c.candidate_id in values),
        key=lambda c: -values[c.candidate_id].net_paise,
    )
    selection: dict[str, float] = {}
    chosen: list[str] = []
    total = 0.0

    for candidate in ordered:
        value = values[candidate.candidate_id]
        if value.net_paise <= 0:
            continue
        trial = {**selection, candidate.candidate_id: 1.0}
        if all(row.satisfied_by(trial) for row in rows):
            selection = trial
            chosen.append(candidate.candidate_id)
            total += value.net_paise

    return Allocation(selected=chosen, objective_paise=total, solver="greedy")


def _offending_rows(rows: list[ConstraintRow]) -> list[str]:
    """Which rows cannot be satisfied even by selecting nothing.

    An LP that is infeasible with an all-zero solution has a row with a negative
    bound, which is a compiler bug rather than a hard problem - and naming it is more
    useful than reporting "infeasible".
    """
    empty: dict[str, float] = {}
    return [row.row_id for row in rows if not row.satisfied_by(empty)]


# ---------------------------------------------------------------------------
# Counterfactual pricing for feasibility-filter constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterfactualPrice:
    """The value of relaxing a constraint that has no dual.

    `TRAI-01` removes candidates before the solver sees them, so there is nothing to
    take a derivative of. The honest way to price it is to re-run the whole pipeline
    with the constraint relaxed and difference the objective.
    """

    label: str
    baseline_objective_paise: float
    relaxed_objective_paise: float
    events: int
    explanation: str

    @property
    def price_paise(self) -> float:
        return self.relaxed_objective_paise - self.baseline_objective_paise

    @property
    def price_per_1000_events_paise(self) -> float:
        return self.price_paise * 1000 / self.events if self.events else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "baseline_objective_rupees": round(self.baseline_objective_paise / 100, 2),
            "relaxed_objective_rupees": round(self.relaxed_objective_paise / 100, 2),
            "price_rupees": round(self.price_paise / 100, 2),
            "price_per_1000_events_rupees": round(self.price_per_1000_events_paise / 100, 2),
            "events": self.events,
            "method": "counterfactual_resolve",
            "explanation": self.explanation,
        }


def summarise_prices(
    allocation: Allocation, counterfactuals: list[CounterfactualPrice] | None = None
) -> dict[str, Any]:
    """The shadow-price panel, in the form the console renders.

    Framing per docs/EVALUATION.md §7.4: this is the price of a rule that exists to
    protect consumers, not an argument against the rule.
    """
    binding = [p for p in allocation.shadow_prices if p.binding and p.price_paise > 0]
    return {
        "framing": (
            "These are the prices of constraints, including consumer-protection rules. "
            "A rule having a cost is not an argument against the rule; it is a fact "
            "about this merchant's operation."
        ),
        "lp_duals": [p.as_dict() for p in binding],
        "slack_rows": [p.row_id for p in allocation.shadow_prices if not p.binding],
        "counterfactual_prices": [c.as_dict() for c in (counterfactuals or [])],
    }
