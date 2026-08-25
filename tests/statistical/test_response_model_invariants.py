"""Invariants the response model must never violate.

Mostly regression guards for docs/POSTMORTEM.md D1-D3. A simulator whose parameters
do not mean what they say produces plausible numbers with the wrong magnitudes, and
nothing downstream objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from antar import clock
from antar.ids import intervention_id
from antar.signals.schemas import (
    CONTACT_CHANNELS,
    Channel,
    FailureClass,
    Intervention,
    MessageClass,
)
from antar.simulator.latents import LatentStore
from antar.simulator.response_model import (
    BASELINE_NOTIFICATIONS,
    MAX_OPTOUT,
    NOTIFICATION_FATIGUE,
    ResponseModel,
)
from antar.simulator.scenarios import REPORTED, get_scenario

pytestmark = pytest.mark.statistical

SEED = 20260822


def contact(channel: Channel, when, *, discount_paise: int = 0) -> Intervention:
    return Intervention(
        intervention_id=intervention_id("evt", channel, when, discount_paise),
        event_id="evt",
        channel=channel,
        message_class=MessageClass.TRANSACTIONAL,
        scheduled_for=when,
        discount_paise=discount_paise,
    )


@pytest.mark.parametrize("scenario_name", REPORTED)
def test_persuadability_is_actually_the_ceiling(scenario_name):
    """docs/POSTMORTEM.md D1.

    `persuadability` is documented as P(persuaded | best channel, best timing, first
    attempt). With no discount offered, no combination of channel, timing, tenure or
    attempt may exceed it.
    """
    scenario = get_scenario(scenario_name)
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)
    base = clock.now()

    violations = []
    for index in range(400):
        latents = store.get(f"cust_{index:06d}")
        for channel in sorted(CONTACT_CHANNELS, key=lambda c: c.value):
            for day_offset in (1, 8, 15, 22, 29):
                when = base + timedelta(days=day_offset, hours=11)
                p = model.persuasion_probability(
                    latents, contact(channel, when), amount_paise=49900, attempt=1
                )
                if p > latents.persuadability + 1e-9:
                    violations.append((latents.customer_id, channel.value, p, latents.persuadability))
    assert not violations, f"{len(violations)} customers exceed their own ceiling: {violations[:3]}"


def test_a_discount_may_exceed_the_messaging_ceiling():
    """The exemption is deliberate and documented: money is a different lever.

    Asserted so the exemption is a decision rather than an oversight.
    """
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000001")
    when = clock.now() + timedelta(hours=25)
    with_discount = model.persuasion_probability(
        latents, contact(Channel.SMS, when, discount_paise=20000), amount_paise=49900, attempt=1
    )
    without = model.persuasion_probability(
        latents, contact(Channel.SMS, when), amount_paise=49900, attempt=1
    )
    assert with_discount > without


def test_probabilities_stay_in_range_across_the_whole_population():
    scenario = get_scenario("conservative")  # the most extreme parameterisation
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)
    base = clock.now()
    for index in range(300):
        latents = store.get(f"cust_{index:06d}")
        for failure_class in FailureClass:
            truth = model.evaluate(
                latents,
                failure_class=failure_class,
                amount_paise=49900,
                next_cycle_at=base + timedelta(days=30),
                intervention=contact(Channel.VOICE, base + timedelta(hours=25)),
            )
            for name, value in (
                ("p_self_heal", truth.p_self_heal),
                ("p_persuaded", truth.p_persuaded),
                ("p_optout", truth.p_optout),
                ("p_recover_treated", truth.p_recover_treated),
                ("p_recover_untreated", truth.p_recover_untreated),
            ):
                assert 0.0 <= value <= 1.0, f"{name}={value} out of range for {failure_class}"


def test_the_control_arm_has_a_real_opt_out_hazard():
    """The counterfactual is **not zero**. docs/EVALUATION.md section 3.3.

    This test used to assert `optout_probability(latents, None) == 0.0`, and in doing so
    encoded the defect as an invariant. Its own docstring said control customers receive
    the pre-debit notification, and then asserted that receiving one carries no risk of
    opting out - when the notification carrying an opt-out route is the entire mechanism
    the project describes. POSTMORTEM D28.

    A test that enforces a bug is worse than no test, because it makes fixing the bug
    look like breaking the code.
    """
    scenario = get_scenario("conservative")
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)

    for index in range(200):
        latents = store.get(f"cust_{index:06d}")
        control = model.optout_probability(latents, None)
        assert control > 0.0, (
            "a control customer receives the mandate's pre-debit notification, which "
            "under RBI-EM-02 must carry a way to cancel. Their hazard cannot be zero."
        )
        assert control <= MAX_OPTOUT


def test_contacting_raises_the_hazard_above_the_control_baseline():
    """The causal effect, as an invariant rather than an assumption.

    Antar's contact must be strictly *worse* than the notification the customer was
    getting anyway, on every channel. If it were not, there would be no harm to price
    and no reason for the system to exist.
    """
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)
    reference = datetime(2026, 4, 2, 11, 4, tzinfo=clock.IST)

    for index in range(100):
        latents = store.get(f"cust_{index:06d}")
        control = model.optout_probability(latents, None)

        for channel in CONTACT_CHANNELS:
            treated = model.optout_probability(
                latents,
                Intervention(
                    intervention_id=f"int_{index}",
                    event_id=f"evt_{index}",
                    channel=channel,
                    message_class=MessageClass.TRANSACTIONAL,
                    scheduled_for=reference,
                ),
            )
            # EMAIL is the one channel less intrusive than SMS, but every contact
            # channel still sits above the bare notification baseline of 0.40.
            assert treated > control, (
                f"{channel.value} produces a hazard of {treated:.4f}, which is not "
                f"above the control baseline of {control:.4f}. Contacting must cost "
                "something or the whole objective is degenerate."
            )


def test_validation_mode_zeroes_both_arms_not_just_one():
    """The one place a zero hazard is correct, and it must apply symmetrically.

    `is_validation_mode` exists to make the anti-circularity harness deterministic. If
    it zeroed only the control arm it would manufacture exactly the artefact D28 was
    about.
    """
    scenario = get_scenario("base")
    if not getattr(scenario, "is_validation_mode", False):
        import dataclasses

        if dataclasses.is_dataclass(scenario):
            try:
                scenario = dataclasses.replace(scenario, is_validation_mode=True)
            except (TypeError, ValueError):
                pytest.skip("scenario is not constructible with is_validation_mode")
        else:
            pytest.skip("scenario is not a dataclass")

    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000001")
    reference = datetime(2026, 4, 2, 11, 4, tzinfo=clock.IST)

    assert model.optout_probability(latents, None) == 0.0
    assert (
        model.optout_probability(
            latents,
            Intervention(
                intervention_id="int_1",
                event_id="evt_1",
                channel=Channel.SMS,
                message_class=MessageClass.TRANSACTIONAL,
                scheduled_for=reference,
            ),
        )
        == 0.0
    )


def test_revoked_mandates_never_recover():
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000001")
    truth = model.evaluate(
        latents,
        failure_class=FailureClass.MANDATE_REVOKED,
        amount_paise=49900,
        next_cycle_at=clock.now() + timedelta(days=30),
        intervention=None,
    )
    assert truth.p_self_heal == 0.0
    assert truth.p_recover_untreated == 0.0


def test_issuer_down_self_heals_far_more_than_a_technical_decline():
    """The whole reason detection matters: an outage passes, an expired card does not.

    If these were similar, WAIT and CONTACT would be interchangeable and L2 would be
    decoration.
    """
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)
    when = clock.now() + timedelta(days=30)
    outage = np.mean(
        [
            model.self_heal_probability(store.get(f"cust_{i:06d}"), FailureClass.ISSUER_DOWN, when)
            for i in range(300)
        ]
    )
    technical = np.mean(
        [
            model.self_heal_probability(
                store.get(f"cust_{i:06d}"), FailureClass.TECHNICAL_DECLINE, when
            )
            for i in range(300)
        ]
    )
    assert outage > 2 * technical, f"issuer_down={outage:.3f} technical={technical:.3f}"


def test_notification_fatigue_raises_the_hazard_monotonically():
    """docs/POSTMORTEM.md D3. Each extra prompt is more dangerous than the last."""
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000003")
    candidate = contact(Channel.SMS, clock.now() + timedelta(hours=25))
    hazards = [
        model.optout_probability(latents, candidate, prior_notifications=n) for n in range(4)
    ]
    assert hazards == sorted(hazards)
    if hazards[0] < MAX_OPTOUT:  # not clipped, so the ratio is meaningful
        assert hazards[1] == pytest.approx(hazards[0] * (1 + NOTIFICATION_FATIGUE), rel=1e-6)


def test_the_baseline_notification_count_is_not_zero():
    """RBI-EM-01 guarantees the failed debit already carried one notification."""
    assert BASELINE_NOTIFICATIONS >= 1


def test_voice_is_more_intrusive_than_email():
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000004")
    when = clock.now() + timedelta(hours=25)
    assert model.optout_probability(latents, contact(Channel.VOICE, when)) > model.optout_probability(
        latents, contact(Channel.EMAIL, when)
    )


def test_a_silent_retry_still_carries_the_full_optout_hazard():
    """The asymmetry that produces sleeping dogs.

    A retry with no dunning message still requires a pre-debit notification, and that
    notification still carries an opt-out. It buys little persuasion and pays full
    price in hazard.
    """
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000005")
    when = clock.now() + timedelta(hours=25)
    silent = contact(Channel.SILENT_RETRY, when)
    sms = contact(Channel.SMS, when)

    assert model.optout_probability(latents, silent) > 0
    assert model.persuasion_probability(
        latents, silent, amount_paise=49900, attempt=1
    ) < model.persuasion_probability(latents, sms, amount_paise=49900, attempt=1)


def test_repeated_attempts_have_diminishing_returns():
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    latents = LatentStore(scenario, SEED).get("cust_000006")
    when = clock.now() + timedelta(hours=25)
    values = [
        model.persuasion_probability(latents, contact(Channel.SMS, when), amount_paise=49900, attempt=a)
        for a in (1, 2, 3, 4)
    ]
    assert values == sorted(values, reverse=True)


def test_timing_against_the_balance_curve_matters():
    """Rescheduling toward the salary peak has to be worth something, or the
    RESCHEDULE branch of the detector is theatre."""
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)
    base = clock.now() + timedelta(hours=24)
    for index in range(50):
        latents = store.get(f"cust_{index:06d}")
        at_peak = latents.next_balance_peak(base)
        at_trough = latents.next_balance_trough(base)
        assert model.persuasion_probability(
            latents, contact(Channel.SMS, at_peak), amount_paise=49900, attempt=1
        ) > model.persuasion_probability(
            latents, contact(Channel.SMS, at_trough), amount_paise=49900, attempt=1
        ), f"{latents.customer_id}: timing has no effect (salary_day={latents.salary_day})"
