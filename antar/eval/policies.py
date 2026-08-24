"""The three policies, run against the same batch under the same constraints.

`docs/EVALUATION.md` §8:

| Policy | What it does | Why it is here |
|---|---|---|
| **P1 contact everyone** | Every feasible cycle gets the default action, subject only to hard limits | The industry default; the honest baseline |
| **P2 propensity targeting** | Target by predicted probability of recovery | **The sophisticated wrong answer.** Targeting the likely-to-recover is not targeting the persuadable |
| **P3 Antar** | Uplift estimate, stopping rules, constrained allocation | The system under test |

**All three face the same contact capacity.** That is the point: a comparison where P1
may send unlimited messages and P3 may not is a comparison of budgets, not of policies.
The interesting question is what each does with the *same* scarce resource.

## Expected value, not sampled outcomes

Value here is computed from the ground-truth response model directly rather than by
drawing outcomes. For the phase diagram that is the right choice: the object of
interest is where the indifference *boundary* sits, and sampling noise at 35-88 grid
cells would blur a boundary we can compute exactly. The headline evaluation in M8 uses
realised outcomes against the randomised control, which is the inferential number.

Both are reported, and which is which is stated wherever either appears.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from antar.decide.allocator import CandidateValue, contact_capacity_row, solve
from antar.policy.compiler import Candidate, CompiledProblem
from antar.signals.schemas import FailureClass, Intervention

POLICIES = ("contact_everyone", "propensity", "antar")


@dataclass
class PolicyOutcome:
    """What one policy achieved on one batch."""

    name: str
    contacts: int = 0
    abstentions: int = 0
    expected_incremental_paise: float = 0.0
    channel_cost_paise: float = 0.0
    expected_optout_loss_paise: float = 0.0
    events: int = 0
    objective_paise: float = 0.0
    shadow_prices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def net_paise(self) -> float:
        return (
            self.expected_incremental_paise
            - self.channel_cost_paise
            - self.expected_optout_loss_paise
        )

    @property
    def net_per_1000_paise(self) -> float:
        return self.net_paise * 1000 / self.events if self.events else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.name,
            "events": self.events,
            "contacts": self.contacts,
            "abstentions": self.abstentions,
            "expected_incremental_rupees": round(self.expected_incremental_paise / 100, 2),
            "channel_cost_rupees": round(self.channel_cost_paise / 100, 2),
            "expected_optout_loss_rupees": round(self.expected_optout_loss_paise / 100, 2),
            "net_rupees": round(self.net_paise / 100, 2),
            "net_per_1000_events_rupees": round(self.net_per_1000_paise / 100, 2),
        }


def value_of(
    response_model: Any,
    latents: Any,
    *,
    event: Any,
    intervention: Intervention | None,
    failure_class: FailureClass,
    next_cycle_at: Any,
    channel_costs: dict[str, int],
    optout_loss_multiplier: float,
) -> CandidateValue:
    """Expected net value of taking `intervention` on `event`, from ground truth.

    Decomposed rather than returned as a single number, because the console shows the
    arithmetic and because a reader should be able to see that the opt-out term is
    what makes some actions negative.
    """
    truth = response_model.evaluate(
        latents,
        failure_class=failure_class,
        amount_paise=event.amount_paise,
        next_cycle_at=next_cycle_at,
        intervention=intervention,
    )
    amount = float(event.amount_paise)
    cost = float(channel_costs.get(intervention.channel.value, 0)) if intervention else 0.0
    return CandidateValue(
        candidate_id=intervention.intervention_id if intervention else f"none::{event.event_id}",
        expected_incremental_paise=truth.uplift * amount,
        channel_cost_paise=cost,
        discount_paise=float(intervention.discount_paise) if intervention else 0.0,
        # The opt-out term. An induced cancellation costs the cycle *and* the mandate
        # behind it, which is why `optout_loss_multiplier` is greater than one and why
        # some perfectly compliant actions are worth less than doing nothing.
        expected_optout_loss_paise=truth.p_optout * amount * optout_loss_multiplier,
    )


class PolicyRunner:
    """Runs all three policies over one batch, under a shared contact capacity."""

    def __init__(
        self,
        batch: Any,
        log: Any,
        *,
        config: Any,
        model: Any = None,
        propensity_model: Any = None,
        detection: Any = None,
    ) -> None:
        self.batch = batch
        self.log = log
        self.config = config
        self.model = model
        self.propensity_model = propensity_model
        # L2's output, and the reason the retention rule can measure anything at all.
        #
        # An earlier version passed `diagnosis=None` throughout, so the detection layer
        # had *no* influence on the allocation - and the L7 retention measurement duly
        # returned a confidence interval of exactly (0, 0) for both components and
        # resolved to DELETE. Deleting a component on a measurement that could not
        # detect its effect would have been a false verdict wearing the clothes of
        # discipline. See docs/POSTMORTEM.md D16.
        self.detection = detection
        self.channel_costs = config.get("simulator.costs.channel_paise")
        self.optout_loss_multiplier = float(config.get("decide.optout_loss_multiplier"))
        self.capacity_fraction = float(config.get("budgets.contact_capacity_fraction"))

        from antar.simulator.response_model import ResponseModel

        self.response = ResponseModel(batch.scenario, batch.seed)

    # ------------------------------------------------------------------ core

    def candidate_values(self) -> tuple[list[Candidate], dict[str, CandidateValue]]:
        """One candidate per event: the single best-valued feasible contact.

        Reducing to one candidate per event keeps the LP at one binary per event, which
        is what lets an 8,000-candidate batch solve inside the M6 budget. Channel
        choice is made here, on expected value; the allocator's job is *whether*, not
        *which*.
        """
        from antar.eval.experiment import ExperimentRunner

        runner = ExperimentRunner(self.batch, config=self.config)
        candidates: list[Candidate] = []
        values: dict[str, CandidateValue] = {}
        by_event = getattr(self.detection, "by_event", {}) or {}

        for event in self.batch.events:
            customer = self.batch.customers[event.customer_id]
            reference = event.occurred_at
            diagnosis = by_event.get(event.event_id)
            hydrated = runner.contacts.hydrate(customer, as_of=reference)
            feasible = runner._feasible_actions(event, hydrated, diagnosis, reference)
            contacts = [a for a in feasible if a is not None]
            if not contacts:
                continue

            latents = self.batch.latents.get(event.customer_id)
            failure_class = self.batch.true_failure_class[event.event_id]
            next_cycle = self.batch.next_cycle_at[event.event_id]

            scored = [
                (
                    action,
                    value_of(
                        self.response,
                        latents,
                        event=event,
                        intervention=action,
                        failure_class=failure_class,
                        next_cycle_at=next_cycle,
                        channel_costs=self.channel_costs,
                        optout_loss_multiplier=self.optout_loss_multiplier,
                    ),
                )
                for action in contacts
            ]
            best_action, best_value = max(scored, key=lambda pair: pair[1].net_paise)

            from antar.signals.schemas import DecisionContext

            context = DecisionContext(
                event=event,
                customer=hydrated,
                intervention=best_action,
                diagnosis=diagnosis,
                now=reference,
                afa_free_ceiling_paise=int(
                    runner.ceilings.get(
                        event.merchant_category.value, runner.ceilings["DEFAULT"]
                    )
                ),
                contact_window_start_hour=int(runner.window["start_hour"]),
                contact_window_end_hour=int(runner.window["end_hour"]),
            )
            candidates.append(Candidate(candidate_id=best_action.intervention_id, context=context))
            values[best_action.intervention_id] = best_value

        return candidates, values

    def capacity(self, n_candidates: int) -> int:
        return max(1, round(self.capacity_fraction * n_candidates))

    def run(self) -> dict[str, PolicyOutcome]:
        candidates, values = self.candidate_values()
        if not candidates:
            return {name: PolicyOutcome(name=name) for name in POLICIES}

        capacity = self.capacity(len(candidates))

        scores = self._policy_scores(candidates)
        # L2 gating applies to Antar alone, and that is the honest representation of
        # the three approaches: "contact everyone" does not diagnose, and a propensity
        # ranker is a pure ML score with no notion of root cause. Only P3 asks *why*
        # the payment failed before deciding whether to ask again.
        vetoed = self._l2_vetoed(candidates)
        outcomes: dict[str, PolicyOutcome] = {}

        for name in POLICIES:
            ranking = scores[name]
            if name == "antar":
                # The only policy that uses the LP, because it is the only one whose
                # value function can be negative - and therefore the only one for
                # which "select nothing here" is a live option the solver can take.
                allowed = [c for c in candidates if c.candidate_id not in vetoed]
                gated = CompiledProblem(feasible=allowed, rows=[])
                allocation = solve(
                    gated,
                    values,
                    extra_rows=[contact_capacity_row(allowed, capacity)],
                    time_limit_seconds=int(self.config.get("decide.allocator.time_limit_seconds")),
                )
                chosen = allocation.selected_set
                shadow = [p.as_dict() for p in allocation.shadow_prices]
                objective = allocation.objective_paise
            else:
                # P1 and P2 rank and fill to capacity. Neither can express harm, so
                # neither has any reason to leave a slot unused.
                ordered = sorted(ranking, key=lambda pair: -pair[1])
                chosen = {cid for cid, _ in ordered[:capacity]}
                shadow = []
                objective = sum(values[cid].net_paise for cid in chosen if cid in values)

            outcome = PolicyOutcome(name=name, events=len(candidates), objective_paise=objective)
            outcome.shadow_prices = shadow
            for candidate in candidates:
                cid = candidate.candidate_id
                if cid not in chosen:
                    outcome.abstentions += 1
                    continue
                value = values[cid]
                outcome.contacts += 1
                outcome.expected_incremental_paise += value.expected_incremental_paise
                outcome.channel_cost_paise += value.channel_cost_paise
                outcome.expected_optout_loss_paise += value.expected_optout_loss_paise
            outcomes[name] = outcome

        return outcomes

    def _l2_vetoed(self, candidates: list[Candidate]) -> set[str]:
        """Candidates the detection layer says not to contact.

        `ISSUER_DOWN` recommends WAIT — the customer could not have paid, and a
        notification would spend an RBI-EM-02 opt-out prompt on a failure that was
        never theirs. `MANDATE_REVOKED` recommends TERMINATE.

        This is the point at which the downtime cross-check and the changepoint
        detector actually reach the money, and therefore the only place their retention
        verdict can be measured. Before this existed the measurement was vacuous.
        """
        from antar.signals.schemas import InterventionClass

        vetoed: set[str] = set()
        for candidate in candidates:
            diagnosis = candidate.context.diagnosis
            if diagnosis is None:
                continue
            if diagnosis.recommended_class in (
                InterventionClass.WAIT,
                InterventionClass.TERMINATE,
            ):
                vetoed.add(candidate.candidate_id)
        return vetoed

    def _policy_scores(self, candidates: list[Candidate]) -> dict[str, list[tuple[str, float]]]:
        """The ranking each policy sorts by."""
        ids = [c.candidate_id for c in candidates]

        everyone = [(cid, 1.0) for cid in ids]

        if self.propensity_model is not None:
            frame = self._features_for(candidates)
            scores = self.propensity_model.predict_uplift(frame)
            propensity = list(zip(ids, [float(s) for s in scores], strict=True))
        else:
            propensity = everyone

        antar = [(cid, 0.0) for cid in ids]  # unused; the LP decides
        return {"contact_everyone": everyone, "propensity": propensity, "antar": antar}

    def _features_for(self, candidates: list[Candidate]) -> pd.DataFrame:
        from antar.decide.features import build_frame, build_row

        rows = [
            build_row(
                c.context.event,
                c.context.customer,
                c.context.diagnosis,
                now=c.context.now,
            )
            for c in candidates
        ]
        return build_frame(rows)


def comparison_table(outcomes: dict[str, PolicyOutcome]) -> list[dict[str, Any]]:
    return [outcomes[name].as_dict() for name in POLICIES if name in outcomes]


def antar_minus_propensity_paise(outcomes: dict[str, PolicyOutcome]) -> float:
    """The number the phase diagram maps.

    P3 minus P2, in net expected rupees per 1,000 at-risk cycles. **Not** P3 minus P1: beating
    "contact everyone" is easy and proves little. `docs/EVALUATION.md` §8.
    """
    antar = outcomes.get("antar")
    propensity = outcomes.get("propensity")
    if antar is None or propensity is None:
        return 0.0
    return antar.net_per_1000_paise - propensity.net_per_1000_paise
