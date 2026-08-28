"""Can we recover an effect we planted?

docs/SIMULATOR_CARD.md section 5.3 and PLAN.md M2 acceptance. In
`scenarios.constant_effect` every customer receives exactly the same additive
treatment effect and all heterogeneity is switched off. A naive difference in means
must recover that constant inside its confidence interval.

**If this fails, nothing downstream is trustworthy and the build stops.** An uplift
model that cannot be validated against an effect we control cannot be believed on an
effect we do not.
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pytest

from antar.ids import intervention_id
from antar.signals.schemas import Channel, FailureClass, Intervention, MessageClass
from antar.simulator.latents import LatentStore
from antar.simulator.response_model import ResponseModel
from antar.simulator.scenarios import CONSTANT_EFFECT

pytestmark = pytest.mark.statistical

SEED = 20260822
N = 6000


def make_intervention(event_id: str, when) -> Intervention:
    return Intervention(
        intervention_id=intervention_id(event_id, Channel.SMS, when, 0),
        event_id=event_id,
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        scheduled_for=when,
        discount_paise=0,
        estimated_cost_paise=25,
    )


def run_experiment(n: int = N) -> tuple[np.ndarray, np.ndarray]:
    """Randomised assignment over `n` synthetic cycles; returns (treated, control)."""
    from antar import clock

    model = ResponseModel(CONSTANT_EFFECT, SEED)
    store = LatentStore(CONSTANT_EFFECT, SEED)
    base = clock.now()

    treated: list[int] = []
    control: list[int] = []

    for index in range(n):
        customer_id = f"cust_{index:06d}"
        event_id = f"evt_ce_{index:06d}"
        latents = store.get(customer_id)
        next_cycle = base + timedelta(days=30)
        # Alternate assignment: a perfectly balanced randomisation, so the test
        # measures the response model rather than an assignment mechanism.
        is_treated = index % 2 == 0
        intervention = (
            make_intervention(event_id, base + timedelta(hours=25)) if is_treated else None
        )
        outcome, _ = model.sample(
            latents,
            event_id=event_id,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_paise=49900,
            next_cycle_at=next_cycle,
            intervention=intervention,
        )
        (treated if is_treated else control).append(int(outcome.recovered))

    return np.array(treated), np.array(control)


def difference_in_means(treated: np.ndarray, control: np.ndarray) -> tuple[float, float, float]:
    """Estimate and a 95% normal-approximation interval for two proportions."""
    diff = float(treated.mean() - control.mean())
    se = math.sqrt(treated.var(ddof=1) / len(treated) + control.var(ddof=1) / len(control))
    return diff, diff - 1.96 * se, diff + 1.96 * se


def test_constant_effect_is_recovered_within_its_ci():
    treated, control = run_experiment()
    planted = CONSTANT_EFFECT.constant_effect
    assert planted is not None

    estimate, low, high = difference_in_means(treated, control)
    assert low <= planted <= high, (
        f"planted effect {planted} outside the recovered CI [{low:.4f}, {high:.4f}] "
        f"(point estimate {estimate:.4f}). The response model and the estimator "
        "disagree; nothing downstream can be trusted until this is resolved."
    )


def test_the_recovered_estimate_is_close_not_merely_inside_a_wide_interval():
    """A CI wide enough to contain anything is not evidence."""
    treated, control = run_experiment()
    estimate, low, high = difference_in_means(treated, control)
    planted = CONSTANT_EFFECT.constant_effect
    assert high - low < 0.06, f"interval too wide to be informative: {high - low:.4f}"
    assert abs(estimate - planted) < 0.02


def test_constant_effect_mode_has_no_heterogeneity():
    """The validation mode must actually switch heterogeneity off.

    If the effect still varies by customer, the difference in means is estimating an
    average rather than the planted constant and the test above proves less than it
    appears to.
    """
    from antar import clock

    model = ResponseModel(CONSTANT_EFFECT, SEED)
    store = LatentStore(CONSTANT_EFFECT, SEED)
    base = clock.now()

    uplifts = []
    for index in range(300):
        latents = store.get(f"cust_{index:06d}")
        truth = model.evaluate(
            latents,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_paise=49900,
            next_cycle_at=base + timedelta(days=30),
            intervention=make_intervention(f"evt_{index}", base + timedelta(hours=25)),
        )
        uplifts.append(truth.uplift)

    spread = float(np.std(uplifts))
    assert spread < 1e-9, f"constant-effect mode still varies by customer (sd={spread:.6f})"
    assert abs(float(np.mean(uplifts)) - CONSTANT_EFFECT.constant_effect) < 1e-9


def test_no_optout_harm_in_validation_mode():
    """Validation mode isolates the persuasion term; the hazard must be off."""
    from antar import clock

    model = ResponseModel(CONSTANT_EFFECT, SEED)
    store = LatentStore(CONSTANT_EFFECT, SEED)
    truth = model.evaluate(
        store.get("cust_000001"),
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        amount_paise=49900,
        next_cycle_at=clock.now() + timedelta(days=30),
        intervention=make_intervention("evt_1", clock.now() + timedelta(hours=25)),
    )
    assert truth.p_optout == 0.0
