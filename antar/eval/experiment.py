"""The batch runner: events in, an experiment log out.

This is the backbone every downstream number rests on. It walks a simulated batch
through detection, arm assignment, action assignment, the policy gate, and the response
model, and emits one row per at-risk event carrying everything the evaluation needs.

## Arm and action assignment

Two independent randomisations, and conflating them is a classic way to ruin an
experiment:

  * **Arm** (`CONTROL` / `TREATMENT`) is sticky per *customer*, from a hash of
    `(customer_id, salt)`. docs/EVALUATION.md §3.1.
  * **Action**, within the treatment arm, is either the policy's choice or - with
    probability ε - drawn **uniformly from the feasible set**. §7 of the simulator card.

## Propensities are known, not estimated

docs/EVALUATION.md §7.1.1. For an exploration event the action was drawn uniformly from
the feasible set, so

    propensity = 1 / |feasible set at assignment time|

exactly. It is a design parameter. Two consequences enforced here:

  * The value is computed **at assignment time and stored on the row**. It is never
    recomputed. `|feasible|` varies per event because the constraint set varies per
    event, and it varies over time as regulations change - so recomputing a weight
    later against a different constraint state would silently corrupt every importance
    weight in the batch, and would do so invisibly.
  * `feasible_set_size` is stored alongside it, so the arithmetic is checkable after
    the fact rather than trusted.

## What the log deliberately does not contain

Any latent. The ground-truth uplift *is* recorded, on a separate frame, because the
evaluation harness is allowed to know the answer - but it never joins into the feature
matrix, and `tests/statistical/test_no_leakage.py` checks the seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from antar.config import Config, get_config
from antar.decide.features import FeatureRow, build_frame, build_row
from antar.detect.pipeline import run_detection
from antar.eval.holdout import Holdout
from antar.ids import intervention_id
from antar.policy.budgets import BudgetPolicy, ContactLedger
from antar.policy.compiler import Candidate, compile_problem
from antar.signals.schemas import (
    Arm,
    AtRiskEvent,
    Channel,
    CustomerContext,
    DecisionContext,
    Diagnosis,
    Intervention,
    MessageClass,
)
from antar.simulator.response_model import ResponseModel
from antar.simulator.rng import substream

# The default action space. `None` - no Antar-initiated action - is always in the set
# and must be, or the uplift models have no untreated treatment-arm rows to learn from.
#
# The *contact* half is configurable (`simulator.available_channels`), because which
# channels a merchant has integrated is a property of the merchant, not of Antar.
DEFAULT_CHANNELS: tuple[Channel, ...] = (
    Channel.SMS,
    Channel.WHATSAPP,
    Channel.EMAIL,
    Channel.VOICE,
)


def available_channels(config) -> tuple[Channel, ...]:
    names = config.get("simulator.available_channels", None)
    if not names:
        return DEFAULT_CHANNELS
    return tuple(Channel(name) for name in names)


@dataclass
class ExperimentLog:
    """One row per at-risk event, plus the ground truth held to one side."""

    features: pd.DataFrame = field(default_factory=pd.DataFrame)
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Assignment, outcome, and propensity. Joined to `features` by position."""

    truth: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Ground-truth uplift per event. **Evaluation only.** Never joined into features."""

    scenario: str = "base"
    seed: int = 0
    diagnoses: dict[str, Diagnosis] = field(default_factory=dict)
    interventions: dict[str, Intervention] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.frame)

    @property
    def treatment_arm(self) -> pd.Series:
        return self.frame["arm"] == Arm.TREATMENT.value

    @property
    def control_arm(self) -> pd.Series:
        return self.frame["arm"] == Arm.CONTROL.value

    @property
    def exploration(self) -> pd.Series:
        return self.frame["is_exploration"]

    def split(self, mask: pd.Series) -> ExperimentLog:
        idx = np.asarray(mask)
        return ExperimentLog(
            features=self.features.loc[idx].reset_index(drop=True),
            frame=self.frame.loc[idx].reset_index(drop=True),
            truth=self.truth.loc[idx].reset_index(drop=True),
            scenario=self.scenario,
            seed=self.seed,
            diagnoses=self.diagnoses,
            interventions=self.interventions,
        )

    def summary(self) -> dict[str, Any]:
        treated = int(self.frame["treated"].sum())
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "events": len(self.frame),
            "control": int(self.control_arm.sum()),
            "treatment": int(self.treatment_arm.sum()),
            "exploration": int(self.exploration.sum()),
            "treated": treated,
            "recovered": int(self.frame["recovered"].sum()),
            "optout": int(self.frame["optout"].sum()),
            "recovered_paise": int(self.frame["recovered_paise"].sum()),
            "mean_propensity": round(float(self.frame["propensity"].mean()), 5),
            "min_propensity": round(float(self.frame["propensity"].min()), 5),
            "mean_feasible_set_size": round(float(self.frame["feasible_set_size"].mean()), 3),
        }


class ExperimentRunner:
    """Runs one batch end to end and returns an `ExperimentLog`."""

    def __init__(
        self,
        batch: Any,
        *,
        config: Config | None = None,
        epsilon: float | None = None,
        policy: str = "uplift",
    ) -> None:
        self.batch = batch
        self.config = config or get_config()
        self.epsilon = (
            epsilon
            if epsilon is not None
            else float(self.config.get("simulator.exploration_epsilon"))
        )
        self.policy = policy
        self.holdout = Holdout(
            control_share=float(self.config.get("eval.control_share")),
            salt=str(self.config.get("eval.experiment_salt")),
        )
        self.budgets = BudgetPolicy.from_config(self.config)
        self.contacts = ContactLedger(policy=self.budgets)
        self.response = ResponseModel(batch.scenario, batch.seed)
        self.ceilings = self.config.get("policy.afa_free_ceiling_paise")
        self.channel_costs = self.config.get("simulator.costs.channel_paise")
        self.window = self.config.get("policy.contact_window")
        self.channels = available_channels(self.config)
        self.lead_hours = int(self.config.get("policy.pre_debit_notification_lead_hours"))

    # ------------------------------------------------------------------ run

    def run(self, *, choose_action: Any = None) -> ExperimentLog:
        detection = run_detection(
            self.batch,
            config=self.config,
            train_event_ids=[e.event_id for e in self.holdout.training_filter(self.batch.events)],
        )

        rows: list[dict[str, Any]] = []
        truth_rows: list[dict[str, Any]] = []
        feature_rows: list[FeatureRow] = []
        interventions: dict[str, Intervention] = {}

        for event in self.batch.events:
            customer = self.batch.customers[event.customer_id]
            diagnosis = detection.by_event.get(event.event_id)
            reference = event.occurred_at
            hydrated = self.contacts.hydrate(customer, as_of=reference)

            feasible = self._feasible_actions(event, hydrated, diagnosis, reference)
            arm = self.holdout.arm_of(event.customer_id)

            chosen, propensity, is_exploration = self._assign(
                event, feasible, arm, choose_action=choose_action, diagnosis=diagnosis
            )

            outcome, ground_truth = self._realise(event, chosen)

            if chosen is not None and chosen.is_contact:
                self.contacts.record(event.customer_id, chosen.scheduled_for)
            if chosen is not None:
                interventions[event.event_id] = chosen

            feature_rows.append(
                build_row(
                    event,
                    hydrated,
                    diagnosis,
                    now=reference,
                    prior_attempts=hydrated.attempts_on_event,
                )
            )
            rows.append(
                {
                    "event_id": event.event_id,
                    "customer_id": event.customer_id,
                    "merchant_id": event.merchant_id,
                    "arm": arm.value,
                    "treated": int(chosen is not None),
                    "channel": chosen.channel.value if chosen else "NONE",
                    "is_exploration": is_exploration,
                    # Logged at assignment time. Never recomputed. EVALUATION 7.1.1.
                    "propensity": propensity,
                    "feasible_set_size": len(feasible),
                    "amount_paise": event.amount_paise,
                    "cost_paise": self._cost_of(chosen),
                    "discount_paise": chosen.discount_paise if chosen else 0,
                    "recovered": int(outcome.recovered),
                    "recovered_paise": outcome.recovered_paise,
                    "optout": int(outcome.optout),
                    "scheduled_for": chosen.scheduled_for if chosen else None,
                    "occurred_at": event.occurred_at,
                }
            )
            truth_rows.append(
                {
                    "event_id": event.event_id,
                    "true_uplift": ground_truth.uplift,
                    "p_self_heal": ground_truth.p_self_heal,
                    "p_persuaded": ground_truth.p_persuaded,
                    "p_optout": ground_truth.p_optout,
                    "true_negative": int(ground_truth.uplift < 0.0),
                }
            )

        return ExperimentLog(
            features=build_frame(feature_rows),
            frame=pd.DataFrame(rows),
            truth=pd.DataFrame(truth_rows),
            scenario=self.batch.scenario.name,
            seed=self.batch.seed,
            diagnoses=detection.by_event,
            interventions=interventions,
        )

    # ------------------------------------------------------- action assembly

    def _candidate(
        self,
        event: AtRiskEvent,
        channel: Channel,
        reference: datetime,
    ) -> Intervention:
        """Build the intervention Antar would schedule on this channel.

        Timing satisfies RBI-EM-01 (>= 24h) and TRAI-01 (inside the contact window) by
        construction, so an action is rejected for a substantive reason rather than
        because we proposed an obviously illegal time.
        """
        from antar import clock

        earliest = reference + timedelta(hours=self.lead_hours)
        scheduled = clock.next_contact_window_start(
            earliest,
            start_hour=int(self.window["start_hour"]),
            end_hour=int(self.window["end_hour"]),
        )
        ceiling = int(self.ceilings.get(event.merchant_category.value, self.ceilings["DEFAULT"]))
        return Intervention(
            intervention_id=intervention_id(event.event_id, channel, scheduled, 0),
            event_id=event.event_id,
            channel=channel,
            message_class=MessageClass.TRANSACTIONAL,
            scheduled_for=scheduled,
            discount_paise=0,
            estimated_cost_paise=int(self.channel_costs.get(channel.value, 0)),
            requires_afa=event.amount_paise > ceiling,
        )

    def _feasible_actions(
        self,
        event: AtRiskEvent,
        customer: CustomerContext,
        diagnosis: Diagnosis | None,
        reference: datetime,
    ) -> list[Intervention | None]:
        """The action set at assignment time. `None` (do nothing) is always feasible.

        This is the set the exploration propensity is computed against, and the reason
        it must be captured now: it depends on the customer's contact history and on
        the regulation set as they were at this instant.
        """
        feasible: list[Intervention | None] = [None]
        candidates: list[Candidate] = []
        by_id: dict[str, Intervention] = {}

        for channel in self.channels:
            intervention = self._candidate(event, channel, reference)
            context = DecisionContext(
                event=event,
                customer=customer,
                intervention=intervention,
                diagnosis=diagnosis,
                now=reference,
                afa_free_ceiling_paise=int(
                    self.ceilings.get(event.merchant_category.value, self.ceilings["DEFAULT"])
                ),
                contact_window_start_hour=int(self.window["start_hour"]),
                contact_window_end_hour=int(self.window["end_hour"]),
                lead_time_hours=self.lead_hours,
                contacts_allowed_per_30d=self.budgets.contacts_per_30d,
                cooldown_hours=self.budgets.cooldown_hours,
            )
            candidates.append(Candidate(candidate_id=intervention.intervention_id, context=context))
            by_id[intervention.intervention_id] = intervention

        problem = compile_problem(
            candidates,
            contacts_per_30d=self.budgets.contacts_per_30d,
        )
        feasible.extend(by_id[c.candidate_id] for c in problem.feasible)
        return feasible

    def _assign(
        self,
        event: AtRiskEvent,
        feasible: Sequence[Intervention | None],
        arm: Arm,
        *,
        choose_action: Any,
        diagnosis: Diagnosis | None,
    ) -> tuple[Intervention | None, float, bool]:
        """Returns (action, propensity, is_exploration).

        Control receives nothing, with propensity 1.0 - it is not a random draw, it is
        the design. Treatment explores with probability epsilon and otherwise follows
        the policy.
        """
        if arm is Arm.CONTROL:
            return None, 1.0, False

        rng = substream(self.batch.seed, "assign", event.event_id)
        if float(rng.random()) < self.epsilon:
            # Treatment is drawn first, at 50/50, and only then a channel uniformly
            # from the feasible contacts.
            #
            # Drawing uniformly over the whole action set - including "do nothing" as
            # one of five options - would put P(treated) at 0.8, and an exploration
            # split that is 80% treated is a poor instrument for estimating a
            # *contrast*: the untreated arm ends up too thin to fit an outcome model
            # on, and the Qini denominator degenerates. Measured at 74% treated before
            # this change. See docs/POSTMORTEM.md D14.
            #
            # The propensity stays exactly known, which is the property that matters
            # (docs/EVALUATION.md 7.1.1). P(treated) = 0.5 for every exploration row;
            # P(this channel | treated) = 1/|feasible contacts|.
            contacts = [action for action in feasible if action is not None]
            if not contacts or float(rng.random()) < 0.5:
                return None, 0.5, True
            index = int(rng.integers(0, len(contacts)))
            return contacts[index], 0.5 / len(contacts), True

        if choose_action is None:
            return None, 1.0, False
        action = choose_action(event, feasible, diagnosis)
        return action, 1.0, False

    def _cost_of(self, action: Intervention | None) -> int:
        if action is None:
            return 0
        return int(self.channel_costs.get(action.channel.value, 0))

    def _realise(self, event: AtRiskEvent, action: Intervention | None):
        assert self.batch.latents is not None
        latents = self.batch.latents.get(event.customer_id)
        true_class = self.batch.true_failure_class[event.event_id]
        next_cycle = self.batch.next_cycle_at[event.event_id]
        return self.response.sample(
            latents,
            event_id=event.event_id,
            failure_class=true_class,
            amount_paise=event.amount_paise,
            next_cycle_at=next_cycle,
            intervention=action,
            attempt=1,
            cost_paise=self._cost_of(action),
        )


def run_experiment(
    batch: Any, *, config: Config | None = None, epsilon: float | None = None, **kwargs
) -> ExperimentLog:
    return ExperimentRunner(batch, config=config, epsilon=epsilon, **kwargs).run()
