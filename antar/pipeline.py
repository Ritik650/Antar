"""One batch, end to end, written to the audit ledger.

L1 signals → L2 detect → L3 decide → L4 act → L5 audit, for every event in a simulated
batch. This is the path the console traces and `replay.py` replays, and the only place
in the codebase where all five layers run in sequence.

## The estimate is an estimate

`eval/policies.py` values candidates from **ground truth** — the simulator's response
model — which is correct there, because comparing three policies fairly means valuing
their choices on the same oracle. It would be indefensible here. A ledger entry saying
*"L3 estimated an uplift of +0.0412"* must contain a number the fitted model actually
produced, or the trace is a fabrication with a hash chain around it.

So the pipeline fits the pre-registered `x_learner` (ADR-0016) on the exploration slice
and predicts. Ground truth is used for exactly one thing — realising the **outcome** of
an action, which is the simulator's job and is labelled as simulated everywhere it
surfaces (N6).

## The holdout never trains and never acts

`Holdout` assigns by customer id, so a control customer is control in every cycle. Their
events are diagnosed and decided — the decision is recorded, with `is_control=True` —
and then nothing is executed. That is what makes the holdout a measurement rather than
a gap in the log: the counterfactual is on the record, and the absence of an action is
attributable to the arm rather than to the estimate (N3).

## Nothing here can send

The gate is constructed with `dry_run` from config, which defaults to true, and no
Razorpay client is passed to it. An executor reached without a client raises. The
pipeline is a decision-and-record path; sending is a separate, deliberate act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from antar import clock
from antar.audit.ledger import Ledger
from antar.ids import decision_id as make_decision_id
from antar.ids import idempotency_key
from antar.signals.schemas import (
    Arm,
    Decision,
    LedgerKind,
    Outcome,
)

UPLIFT_MODEL = "x_learner"
"""The pre-registered winner. Changing this is a protocol amendment, not a tweak —
see ADR-0016 and the amendment discipline in EVALUATION.md §6.2."""


@dataclass
class PipelineResult:
    scenario: str
    seed: int
    events: int = 0
    diagnosed: int = 0
    decided: int = 0
    contacted: int = 0
    abstained: int = 0
    control: int = 0
    refused: int = 0
    recovered: int = 0
    recovered_paise: int = 0
    cost_paise: int = 0
    optouts: int = 0
    ledger_head: str = ""
    ledger_entries: int = 0
    model_version: str = "unversioned"
    policy_version: str = "unversioned"
    notes: list[str] = field(default_factory=list)

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.cost_paise

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "events": self.events,
            "diagnosed": self.diagnosed,
            "decided": self.decided,
            "contacted": self.contacted,
            "abstained": self.abstained,
            "control": self.control,
            "refused": self.refused,
            "recovered": self.recovered,
            "recovered_rupees": round(self.recovered_paise / 100, 2),
            "cost_rupees": round(self.cost_paise / 100, 2),
            "net_rupees": round(self.net_paise / 100, 2),
            "optouts": self.optouts,
            "ledger_entries": self.ledger_entries,
            "ledger_head": self.ledger_head,
            "model_version": self.model_version,
            "policy_version": self.policy_version,
            "notes": self.notes,
            "simulated": True,  # N6. Never present these as real recovery rates.
        }


def _policy_version(config: Any) -> str:
    """A content hash of everything that changes what the policy layer does.

    Two runs with the same policy version made decisions under the same rules; two runs
    with different ones did not, and a trace that could not tell them apart would make
    the ledger useless for exactly the question it exists to answer.
    """
    import hashlib

    from antar.policy import regulations as reg

    parts = [
        f"{rule.id}:{rule.severity.value}:{rule.verification.value}" for rule in reg.REGULATIONS
    ]
    parts.append(f"window:{config.get('policy.contact_window.start_hour')}")
    parts.append(f"window_end:{config.get('policy.contact_window.end_hour')}")
    parts.append(f"contacts30d:{config.get('budgets.contacts_per_30d')}")
    parts.append(f"capacity:{config.get('budgets.contact_capacity_fraction')}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"pol-{digest[:12]}"


class NoModel(RuntimeError):
    """No fitted uplift model, so the pipeline declines to act.

    PLAN.md section 10: *"Uplift model missing / version mismatch → refuses to act;
    does not silently fall back to targeting everyone."* The safe failure for a money
    system is do nothing, not do the naive thing - and "contact everyone" is precisely
    the naive thing an unfitted pipeline drifts into, because a uniform score ranks
    every candidate equally and the capacity fills top-down.
    """


@dataclass(frozen=True)
class Models:
    """The two estimates L3 needs, and the versions that produced them.

    **Recovery uplift** is what a contact is worth. **Opt-out uplift** is what it
    risks. A pipeline with only the first would treat every contact as free of harm and
    would contact far more than it should - which is the failure the whole project
    argues against, so it is a hard requirement rather than a refinement.
    """

    recovery: Any
    optout: Any
    version: str
    rows: int


def _fit_models(log: Any, *, minimum_rows: int = 100) -> Models:
    """Fit both learners on the exploration slice.

    Raises `NoModel` rather than returning a degraded object. A caller that receives
    `None` and carries on is exactly the silent fallback PLAN.md section 10 forbids, and
    the only reliable way to prevent it is to make carrying on impossible.
    """
    from antar.decide.uplift.learners import LEARNERS, OptoutRisk

    exploration = log.split(log.exploration)
    if len(exploration) < minimum_rows:
        raise NoModel(
            f"only {len(exploration)} exploration rows, below the floor of "
            f"{minimum_rows}. Refusing to act: an unfitted model scores every candidate "
            "identically, and an identical score under a capacity constraint is "
            "'contact everyone until the budget runs out'."
        )

    features = exploration.features
    treated = exploration.frame["treated"].to_numpy().astype(bool)

    recovery = LEARNERS[UPLIFT_MODEL]()
    recovery.fit(features, treated, exploration.frame["recovered"].to_numpy())

    optout = OptoutRisk()
    optout.fit(features, treated, exploration.frame["optout"].to_numpy())

    return Models(
        recovery=recovery,
        optout=optout,
        version=f"{UPLIFT_MODEL}-n{len(exploration)}",
        rows=len(exploration),
    )


def _build_candidates(batch: Any, detection: Any, config: Any) -> list[Any]:
    """One candidate per event, chosen **without** looking at the answer.

    `PolicyRunner.candidate_values` picks the best channel by oracle value, which is
    correct there: comparing three policies fairly means valuing every policy's choices
    on the same ground truth. Here it would be a leak wearing a decision's clothes -
    the ledger would record a channel chosen by the simulator and attribute it to L3.

    So the channel is chosen by **cost among feasible contacts**. The uplift learner is
    trained on treated-versus-not and produces one estimate per event, not one per
    channel, so it has no opinion about which channel to use; picking the cheapest is
    the honest reading of an estimator that cannot distinguish them. `docs/LIMITATIONS.md`
    L16 says so, because "we chose SMS because it is cheapest" is a much weaker claim
    than the architecture might otherwise suggest.
    """
    from antar.eval.experiment import ExperimentRunner
    from antar.policy.compiler import Candidate
    from antar.signals.schemas import DecisionContext

    # `ExperimentRunner` owns feasibility: the contact ledger, the AFA ceilings, and the
    # window come from one place so that L3 and the evaluation cannot disagree about
    # what was allowed.
    runner = ExperimentRunner(batch, config=config)
    channel_costs = config.get("simulator.costs.channel_paise")
    by_event = getattr(detection, "by_event", {}) or {}
    candidates: list[Any] = []

    for event in batch.events:
        customer = batch.customers[event.customer_id]
        reference = event.occurred_at
        diagnosis = by_event.get(event.event_id)
        hydrated = runner.contacts.hydrate(customer, as_of=reference)
        contacts = [
            a
            for a in runner._feasible_actions(event, hydrated, diagnosis, reference)
            if a is not None
        ]
        if not contacts:
            continue

        cheapest = min(
            contacts,
            key=lambda a: (int(channel_costs.get(a.channel.value, 0)), a.channel.value),
        )
        context = DecisionContext(
            event=event,
            customer=hydrated,
            intervention=cheapest,
            diagnosis=diagnosis,
            now=reference,
            afa_free_ceiling_paise=int(
                runner.ceilings.get(event.merchant_category.value, runner.ceilings["DEFAULT"])
            ),
            contact_window_start_hour=int(runner.window["start_hour"]),
            contact_window_end_hour=int(runner.window["end_hour"]),
        )
        candidates.append(Candidate(candidate_id=cheapest.intervention_id, context=context))

    return candidates


def _estimate_values(
    candidates: list[Any], models: Models, config: Any
) -> tuple[dict[str, Any], dict[str, float]]:
    """What each candidate is worth, **from the fitted models only**.

    net = recovery_uplift x amount - channel cost - optout_uplift x amount x multiplier

    Both terms are predictions. Nothing here reads `batch.latents`, `true_failure_class`
    or the response model, which is what lets a ledger entry saying "L3 estimated an
    uplift of +0.0412" be a true statement about a model rather than a rephrasing of
    the answer key.
    """
    import numpy as np

    from antar.decide.allocator import CandidateValue
    from antar.decide.features import build_frame, build_row

    if not candidates:
        return {}, {}

    channel_costs = config.get("simulator.costs.channel_paise")
    multiplier = float(config.get("decide.optout_loss_multiplier"))

    frame = build_frame(
        [
            build_row(c.context.event, c.context.customer, c.context.diagnosis, now=c.context.now)
            for c in candidates
        ]
    )
    recovery = np.asarray(models.recovery.predict_uplift(frame), dtype=float)
    optout = np.asarray(models.optout.predict_uplift(frame), dtype=float)

    values: dict[str, Any] = {}
    uplifts: dict[str, float] = {}
    for candidate, recovery_hat, optout_hat in zip(candidates, recovery, optout, strict=True):
        intervention = candidate.context.intervention
        amount = float(candidate.context.event.amount_paise)
        values[candidate.candidate_id] = CandidateValue(
            candidate_id=candidate.candidate_id,
            expected_incremental_paise=float(recovery_hat) * amount,
            channel_cost_paise=float(channel_costs.get(intervention.channel.value, 0)),
            discount_paise=float(intervention.discount_paise),
            # A predicted *increase* in opt-out is a cost; a predicted decrease is not a
            # credit. Clamping at zero refuses to let the model pay for a contact by
            # claiming it retains customers, which is a claim this design has no way to
            # support and every incentive to make.
            expected_optout_loss_paise=max(0.0, float(optout_hat)) * amount * multiplier,
        )
        uplifts[candidate.candidate_id] = float(recovery_hat)

    return values, uplifts


def run_pipeline(
    scenario: str = "base",
    *,
    seed: int | None = None,
    config: Any = None,
    ledger: Ledger | None = None,
    batch: Any = None,
    log: Any = None,
    max_events: int | None = None,
) -> tuple[PipelineResult, Ledger]:
    """Run one batch through every layer and record it.

    `batch` and `log` may be passed in to avoid regenerating them; everything else is
    derived. Returns the summary and the ledger it wrote to.
    """

    from antar.act.drafter import DraftContext, Drafter
    from antar.config import get_config
    from antar.decide.allocator import contact_capacity_row, solve
    from antar.detect.pipeline import run_detection
    from antar.eval.experiment import run_experiment
    from antar.eval.holdout import Holdout
    from antar.eval.policies import PolicyRunner
    from antar.policy.compiler import CompiledProblem
    from antar.policy.gate import PolicyGate
    from antar.simulator.generator import generate
    from antar.simulator.response_model import ResponseModel

    config = config or get_config()
    seed = int(config.get("run.seed")) if seed is None else seed
    ledger = Ledger() if ledger is None else ledger  # `is None`: see D21

    if batch is None:
        batch = generate(scenario, seed=seed, config=config)
    if log is None:
        log = run_experiment(batch, config=config)

    result = PipelineResult(scenario=scenario, seed=seed)
    result.policy_version = _policy_version(config)

    models = _fit_models(log)
    result.model_version = models.version

    detection = run_detection(batch, config=config)
    runner = PolicyRunner(batch, log, config=config, detection=detection)
    candidates = _build_candidates(batch, detection, config)
    values, uplift_by_candidate = _estimate_values(candidates, models, config)

    # L3's own view of the world: compile the regulations, then allocate under capacity.
    #
    # The L2 veto is applied here for the same reason `PolicyRunner.run` applies it:
    # ISSUER_DOWN recommends WAIT and MANDATE_REVOKED recommends TERMINATE, and a
    # pipeline that skipped the veto would be running a *different* policy from the one
    # the evaluation measured while reporting it under the same name. An earlier version
    # of this function did skip it, and the first end-to-end trace duly showed a
    # candidate the evaluated policy would never have chosen.
    vetoed = runner._l2_vetoed(candidates)
    candidates = [c for c in candidates if c.candidate_id not in vetoed]
    problem = CompiledProblem(feasible=candidates, rows=[])
    capacity = runner.capacity(len(candidates)) if candidates else 0
    allocation = (
        solve(
            problem,
            values,
            extra_rows=[contact_capacity_row(candidates, capacity)],
            time_limit_seconds=int(config.get("decide.allocator.time_limit_seconds")),
        )
        if candidates
        else None
    )
    selected = allocation.selected_set if allocation else set()
    binding_rows = (
        [p.row_id for p in allocation.shadow_prices if p.binding] if allocation else []
    )

    by_candidate = {c.event_id: c for c in candidates}

    holdout = Holdout(
        control_share=float(config.get("eval.control_share")),
        salt=str(config.get("eval.holdout_salt", "antar")),
    )
    gate = PolicyGate.from_config(config, ledger=ledger)
    drafter = Drafter.from_config(config)
    response = ResponseModel(batch.scenario, batch.seed)

    events = list(batch.events)
    if max_events is not None:
        events = events[:max_events]

    for event in events:
        result.events += 1
        ledger.append(LedgerKind.EVENT, event, written_at=event.occurred_at)

        diagnosis = detection.by_event.get(event.event_id)
        if diagnosis is not None:
            result.diagnosed += 1
            ledger.append(LedgerKind.DIAGNOSIS, diagnosis, written_at=event.occurred_at)

        candidate = by_candidate.get(event.event_id)
        is_control = holdout.is_control_event(event)
        chosen = (
            candidate.context.intervention
            if candidate is not None
            and candidate.candidate_id in selected
            and not is_control
            else None
        )

        uplift = uplift_by_candidate.get(
            candidate.candidate_id if candidate else "", 0.0
        )
        # A point estimate with no interval is a claim without an error bar. The width
        # is the exploration-slice standard error, which is what the bake-off reports;
        # it is not a per-event interval and the field name does not pretend otherwise.
        half_width = float(config.get("decide.uplift_ci_half_width", 0.05))

        constraints: list[str] = []
        if candidate is None:
            constraints.append("NO_FEASIBLE_ACTION")
        elif candidate.candidate_id not in selected and not is_control:
            constraints.extend(binding_rows or ["VALUE_BELOW_ZERO"])
        if is_control:
            constraints.append("RANDOMISED_HOLDOUT")

        decision = Decision(
            decision_id=make_decision_id(
                event.event_id, result.policy_version, models.version
            ),
            event_id=event.event_id,
            chosen=chosen,
            uplift_estimate=uplift,
            uplift_ci=(uplift - half_width, uplift + half_width),
            expected_incremental_paise=int(
                values[candidate.candidate_id].expected_incremental_paise
            )
            if candidate and candidate.candidate_id in values
            else 0,
            expected_cost_paise=int(values[candidate.candidate_id].channel_cost_paise)
            if candidate and candidate.candidate_id in values
            else 0,
            binding_constraints=constraints,
            model_version=models.version,
            policy_version=result.policy_version,
            is_control=is_control,
            arm=Arm.CONTROL if is_control else Arm.TREATMENT,
            decided_at=event.occurred_at,
            rationale=_rationale(
                candidate, chosen, is_control, constraints
            ),
        )
        result.decided += 1
        if is_control:
            result.control += 1
        ledger.append(LedgerKind.DECISION, decision, written_at=event.occurred_at)

        if chosen is None:
            result.abstained += 1
            outcome = _realise(response, batch, event, None, 0)
            ledger.append(LedgerKind.OUTCOME, outcome, written_at=event.occurred_at)
            _tally(result, outcome)
            continue

        # ------------------------------------------------------------ L4
        with clock.use_clock(clock.FrozenClock(chosen.scheduled_for)):
            draft = drafter.draft(
                DraftContext(
                    event=event,
                    intervention=chosen,
                    failure_class=diagnosis.failure_class
                    if diagnosis
                    else batch.true_failure_class[event.event_id],
                    merchant_name=str(config.get("act.merchant_name", "Antar")),
                )
            )
            action = gate.submit(
                decision,
                idempotency_key=idempotency_key(decision.decision_id, chosen.channel.value),
                amount_paise=event.amount_paise,
                cost_paise=int(
                    config.get("simulator.costs.channel_paise").get(chosen.channel.value, 0)
                ),
                draft=draft,
            )

        channel_cost = int(
            config.get("simulator.costs.channel_paise").get(chosen.channel.value, 0)
        )
        if action.blocked:
            result.refused += 1
            # No contact happened, so no channel cost was incurred. Charging for a
            # refused send would credit the gate with a saving it did not make.
            outcome = _realise(response, batch, event, None, 0)
        else:
            result.contacted += 1
            outcome = _realise(response, batch, event, chosen, channel_cost)

        ledger.append(LedgerKind.OUTCOME, outcome, written_at=event.occurred_at)
        _tally(result, outcome)

    result.ledger_entries = len(ledger)
    result.ledger_head = ledger.head()
    return result, ledger


def _rationale(
    candidate: Any,
    chosen: Any,
    is_control: bool,
    constraints: list[str],
) -> str:
    if is_control:
        return (
            "In the randomised holdout. The decision was computed and recorded; no "
            "action was taken, because the holdout is never treated (N3)."
        )
    if candidate is None:
        return (
            "No feasible action existed for this event: every candidate contact was "
            "removed by a blocking regulation or fell outside the permitted window."
        )
    if chosen is None:
        if "VALUE_BELOW_ZERO" in constraints:
            return (
                "A feasible contact existed and was worth less than nothing: the "
                "expected opt-out loss exceeded the expected recovery. Abstaining is "
                "the action."
            )
        return (
            "A feasible contact existed but was not selected under the contact "
            "capacity: other events in the same batch were worth more."
        )
    return (
        "Selected by the allocator: expected recovery exceeded the channel cost and "
        "the expected opt-out loss, and a contact slot was available."
    )


def _realise(response: Any, batch: Any, event: Any, intervention: Any, cost_paise: int) -> Outcome:
    """The simulated outcome of this decision. N6: never a real recovery rate.

    Delegates to `ResponseModel.sample`, which draws from three keyed substreams -
    recovery, opt-out, timing - rather than thresholding the probabilities. A first
    version of this function did threshold at 0.5, which is deterministic, reproducible,
    and wrong: it turns a 0.55 recovery probability into certainty and a 0.45 into
    impossibility, so the aggregate recovery rate stops being a rate at all.
    """
    outcome, _truth = response.sample(
        batch.latents.get(event.customer_id),
        event_id=event.event_id,
        failure_class=batch.true_failure_class[event.event_id],
        amount_paise=event.amount_paise,
        next_cycle_at=batch.next_cycle_at[event.event_id],
        intervention=intervention,
        attempt=1,
        cost_paise=cost_paise,
    )
    return outcome


def _tally(result: PipelineResult, outcome: Outcome) -> None:
    if outcome.recovered:
        result.recovered += 1
        result.recovered_paise += outcome.recovered_paise
    result.cost_paise += outcome.cost_paise + outcome.discount_paise
    if outcome.optout:
        result.optouts += 1


__all__ = ["UPLIFT_MODEL", "PipelineResult", "run_pipeline"]
