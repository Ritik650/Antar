"""Ground truth: what actually happens to a customer, given what we did.

docs/SIMULATOR_CARD.md section 5 is the specification. The structure:

    P(recover | intervention) = P(self_heal)
                              + (1 - P(self_heal)) x P(persuaded | intervention)
                              - P(optout_induced | intervention)

The middle term is the only part an intervention can move upward. The last term is
the harm it can cause. **The incremental effect is the difference between this and
the same expression with intervention = NONE**, which is exactly the quantity the
uplift estimators in `antar/decide/uplift/` have to recover.

### The opt-out hazard

Read SIMULATOR_CARD section 6 before judging this. Under RBI-EM-01 a pre-transaction
notification must precede every debit by 24 hours, and under RBI-EM-02 it must carry
a facility to opt out of that transaction or the mandate. **The notification is
therefore also a cancellation prompt.** That is not our invention - it is what the
framework requires.

So the hazard is attached to the *notification event*, not to a pre-labelled segment.
There is no `is_sleeping_dog` flag in this file or anywhere else in the package. A
customer becomes a sleeping dog emergently, when their `optout_sensitivity` and
`intent_to_churn` are high enough that the expected opt-out loss exceeds the expected
persuasion gain. `tests/statistical/test_anti_circularity.py` checks that this
population appears in at least two of the three scenarios without any scenario
touching a dial that names it.

What we cannot defend is the magnitude. We chose the functional form of `g` and the
distribution of `optout_sensitivity`; a different choice gives a different population,
possibly none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from antar.signals.schemas import (
    Channel,
    FailureClass,
    Intervention,
    Outcome,
)
from antar.simulator.latents import CustomerLatents
from antar.simulator.rng import substream
from antar.simulator.scenarios import Scenario

# ---------------------------------------------------------------------------
# Author-chosen constants. Every one of these is invented. They are gathered here
# rather than scattered through the code so that the simulator card can point at a
# single place and so that nobody can quietly tune one inside a branch.
# ---------------------------------------------------------------------------

SELF_HEAL_BY_CLASS: dict[FailureClass, float] = {
    # An outage ends. The customer was never the problem, so the next scheduled
    # cycle mostly succeeds on its own - which is why contacting them is waste.
    FailureClass.ISSUER_DOWN: 0.75,
    # Depends on the balance curve at the next cycle; the multiplier is applied on
    # top of the timing term below.
    FailureClass.INSUFFICIENT_FUNDS: 1.00,
    # An expired card does not fix itself.
    FailureClass.TECHNICAL_DECLINE: 0.35,
    FailureClass.RISK_DECLINE: 0.55,
    # Requires the customer to complete an authentication step.
    FailureClass.AFA_REQUIRED: 0.20,
    # Terminal. No amount of anything recovers this cycle.
    FailureClass.MANDATE_REVOKED: 0.00,
    FailureClass.UNKNOWN: 0.60,
}

# How much of the persuasion ceiling each intervention class can reach.
CHANNEL_INTRUSIVENESS: dict[Channel, float] = {
    Channel.SMS: 1.00,
    Channel.WHATSAPP: 1.10,
    Channel.VOICE: 1.45,  # a phone call provokes more cancellations than a message
    Channel.EMAIL: 0.70,
    Channel.SILENT_RETRY: 0.85,
}

BASE_OPTOUT_HAZARD = 0.075
"""P(opt-out | one notification, reference sensitivity, no churn intent). Invented."""

REFERENCE_OPTOUT_SENSITIVITY = 0.18
"""The sensitivity at which `BASE_OPTOUT_HAZARD` is exactly the hazard.

Fixed, and deliberately **not** read from the scenario. An earlier version divided
by `scenario.mean_optout_sensitivity`, which cancelled the scenario axis exactly: the
mean hazard came out identical in all three parameterisations even though
docs/SIMULATOR_CARD.md section 8 advertises opt-out sensitivity as one of the four
things that distinguishes them. See docs/POSTMORTEM.md D2. This constant happens to
equal the base scenario's mean, so base is the reference regime by construction."""

CHURN_INTENT_MULTIPLIER = 2.6
"""How much a latent desire to cancel amplifies the response to a cancel button."""

NOTIFICATION_FATIGUE = 0.40
"""Each prior notification in the window raises the hazard by this fraction."""

BASELINE_NOTIFICATIONS = 1
"""Notifications a customer has already received when their cycle fails.

Under RBI-EM-01 every debit attempt is preceded by a pre-transaction notification,
so the cycle that just failed already consumed one. An Antar-initiated retry is
therefore never the customer's first cancel prompt of the month - it is at least the
second, and fatigue applies. This is the default rather than a value callers pass,
because an earlier version defaulted to zero and understated the hazard on every
event in the batch. See docs/POSTMORTEM.md D3."""

SILENT_RETRY_NUDGE = 0.22
"""A mandatory pre-debit notification is itself a mild reminder, so even a retry
with no dunning message carries some persuasive effect. It also carries the full
opt-out hazard, which is the asymmetry that produces sleeping dogs."""

ATTEMPT_DECAY = 0.72
"""Diminishing returns on repeated attempts, then annoyance."""

MAX_PERSUASION = 0.92
MAX_OPTOUT = 0.55


@dataclass(frozen=True)
class GroundTruth:
    """The true probabilities behind one (customer, cycle, intervention) triple.

    Visible to the simulator and the evaluation harness only. Never to a feature
    builder - see `tests/statistical/test_no_leakage.py`.
    """

    p_self_heal: float
    p_persuaded: float
    p_optout: float
    p_recover_treated: float
    p_recover_untreated: float

    @property
    def uplift(self) -> float:
        """The ground-truth individual treatment effect on recovery probability."""
        return self.p_recover_treated - self.p_recover_untreated

    @property
    def has_negative_uplift(self) -> bool:
        """Derived, never assigned.

        Named for what it computes rather than for the finding it supports. A field
        called `is_sleeping_dog` would be indistinguishable, to a reader, from a
        planted label - and `test_anti_circularity` rejects any such identifier in
        this package.
        """
        return self.uplift < 0.0


class ResponseModel:
    """Samples ground-truth outcomes. The only place the answer key is consulted."""

    def __init__(self, scenario: Scenario, seed: int) -> None:
        self.scenario = scenario
        self.seed = seed

    # ----------------------------------------------------------- components

    def self_heal_probability(
        self,
        latents: CustomerLatents,
        failure_class: FailureClass,
        next_cycle_at: datetime,
    ) -> float:
        base = latents.p_self_heal_base * SELF_HEAL_BY_CLASS.get(failure_class, 0.6)
        if failure_class is FailureClass.INSUFFICIENT_FUNDS:
            # The account either refills by the next cycle or it does not. This is
            # what makes rescheduling toward the salary peak a real lever rather
            # than a story.
            base *= 0.35 + 0.95 * latents.balance_fraction(next_cycle_at)
        return float(min(max(base, 0.0), 0.98))

    def persuasion_probability(
        self,
        latents: CustomerLatents,
        intervention: Intervention | None,
        *,
        amount_paise: int,
        attempt: int,
    ) -> float:
        if intervention is None:
            return 0.0
        if self.scenario.is_validation_mode:
            # Constant-effect mode: one number, no heterogeneity, nothing else.
            return float(self.scenario.constant_effect or 0.0)

        # Every modifier below is confined to (0, 1], so that `persuadability` is
        # genuinely the ceiling its docstring claims: P(persuaded | best channel,
        # best timing, first attempt). An earlier version let these stack above 1.0,
        # which put the median dunning SMS at a 43% success rate - see
        # docs/POSTMORTEM.md D1. The invariant is asserted by
        # tests/statistical/test_response_model_invariants.py.
        if intervention.channel is Channel.SILENT_RETRY:
            channel_fit = SILENT_RETRY_NUDGE
        else:
            # channel_response sums to 1 across four channels. A customer who puts
            # half their responsiveness on one channel is fully aligned with it.
            channel_fit = min(latents.channel_response[intervention.channel] / 0.50, 1.0)

        timing = 0.25 + 0.75 * latents.balance_fraction(intervention.scheduled_for)
        attempt_decay = ATTEMPT_DECAY ** max(attempt - 1, 0)
        tenure_factor = 0.75 + 0.25 * min(latents.tenure_months / 24.0, 1.0)

        persuaded = latents.persuadability * channel_fit * timing * attempt_decay * tenure_factor

        if intervention.discount_paise > 0 and amount_paise > 0:
            # A discount is an economic lever rather than a messaging one, so it
            # adds on top of the messaging ceiling rather than being bounded by it.
            discount_fraction = intervention.discount_paise / amount_paise
            persuaded += latents.price_sensitivity * discount_fraction * 0.55

        return float(min(max(persuaded, 0.0), MAX_PERSUASION))

    def optout_probability(
        self,
        latents: CustomerLatents,
        intervention: Intervention | None,
        *,
        prior_notifications: int = BASELINE_NOTIFICATIONS,
    ) -> float:
        """g(optout_sensitivity, intent_to_churn, notifications_received_recently).

        Note what this function does *not* take: a segment label, a customer
        category, anything that could carry a planted answer. It is a function of
        the notification event and two latents.
        """
        if intervention is None:
            # The counterfactual. Control customers still receive the pre-debit
            # notification for their *scheduled* debit, because that is the issuer's
            # obligation rather than our action - but they receive no additional
            # Antar-initiated one. docs/EVALUATION.md section 3.2.
            return 0.0
        if self.scenario.is_validation_mode:
            return 0.0

        hazard = BASE_OPTOUT_HAZARD * latents.optout_sensitivity / REFERENCE_OPTOUT_SENSITIVITY
        hazard *= CHANNEL_INTRUSIVENESS[intervention.channel]
        if latents.intent_to_churn:
            hazard *= CHURN_INTENT_MULTIPLIER
        hazard *= 1.0 + NOTIFICATION_FATIGUE * prior_notifications
        return float(min(max(hazard, 0.0), MAX_OPTOUT))

    # --------------------------------------------------------------- assembly

    def evaluate(
        self,
        latents: CustomerLatents,
        *,
        failure_class: FailureClass,
        amount_paise: int,
        next_cycle_at: datetime,
        intervention: Intervention | None,
        attempt: int = 1,
        prior_notifications: int = BASELINE_NOTIFICATIONS,
    ) -> GroundTruth:
        """The true probabilities, with no sampling. Used by the anti-circularity
        test, which needs the population's uplift distribution rather than a draw
        from it."""
        p_self_heal = self.self_heal_probability(latents, failure_class, next_cycle_at)
        p_persuaded = self.persuasion_probability(
            latents, intervention, amount_paise=amount_paise, attempt=attempt
        )
        p_optout = self.optout_probability(
            latents, intervention, prior_notifications=prior_notifications
        )

        # The card writes this as `p_self_heal + (1-p_self_heal)*p_persuaded -
        # p_optout`. We use the exact factorisation, of which that expression is the
        # first-order expansion: an opt-out ends the mandate, so recovery is
        # conditional on it not happening. See SIMULATOR_CARD changelog 2026-08-22.
        p_conditional = p_self_heal + (1.0 - p_self_heal) * p_persuaded
        p_treated = (1.0 - p_optout) * p_conditional

        if self.scenario.is_validation_mode and intervention is not None:
            # Plant the effect exactly, so a difference in means has something
            # unambiguous to recover.
            p_treated = min(p_self_heal + float(self.scenario.constant_effect or 0.0), 1.0)

        return GroundTruth(
            p_self_heal=p_self_heal,
            p_persuaded=p_persuaded,
            p_optout=p_optout,
            p_recover_treated=float(min(max(p_treated, 0.0), 1.0)),
            p_recover_untreated=p_self_heal,
        )

    def sample(
        self,
        latents: CustomerLatents,
        *,
        event_id: str,
        failure_class: FailureClass,
        amount_paise: int,
        next_cycle_at: datetime,
        intervention: Intervention | None,
        attempt: int = 1,
        prior_notifications: int = BASELINE_NOTIFICATIONS,
        cost_paise: int = 0,
        measurement_window_days: int = 30,
    ) -> tuple[Outcome, GroundTruth]:
        """Draw the realised outcome for one at-risk event."""
        truth = self.evaluate(
            latents,
            failure_class=failure_class,
            amount_paise=amount_paise,
            next_cycle_at=next_cycle_at,
            intervention=intervention,
            attempt=attempt,
            prior_notifications=prior_notifications,
        )

        # Two independent substreams, so that adding an opt-out draw does not shift
        # the recovery draw and silently change every historical result.
        optout_rng = substream(self.seed, "outcome.optout", event_id)
        recover_rng = substream(self.seed, "outcome.recover", event_id)
        timing_rng = substream(self.seed, "outcome.timing", event_id)

        opted_out = bool(optout_rng.random() < truth.p_optout)
        if opted_out:
            recovered = False
        else:
            p_conditional = truth.p_self_heal + (1.0 - truth.p_self_heal) * truth.p_persuaded
            if self.scenario.is_validation_mode and intervention is not None:
                p_conditional = truth.p_recover_treated
            recovered = bool(recover_rng.random() < p_conditional)

        discount = intervention.discount_paise if (intervention and recovered) else 0
        recovered_paise = max(amount_paise - discount, 0) if recovered else 0

        recovered_at: datetime | None = None
        if recovered:
            # Uniform over the measurement window, biased early by the square root -
            # most recoveries that happen at all happen soon.
            fraction = float(timing_rng.random()) ** 2
            recovered_at = next_cycle_at + timedelta(days=fraction * measurement_window_days)

        is_contact = intervention is not None and intervention.is_contact
        return (
            Outcome(
                event_id=event_id,
                recovered=recovered,
                recovered_paise=recovered_paise,
                recovered_at=recovered_at,
                optout=opted_out,
                optout_at=next_cycle_at if opted_out else None,
                mandate_revoked=opted_out,
                attempts=attempt if intervention is not None else 0,
                contacts=1 if is_contact else 0,
                cost_paise=cost_paise,
                discount_paise=discount,
            ),
            truth,
        )
