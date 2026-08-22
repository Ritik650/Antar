"""The detection layer: taxonomy, changepoint, mandate FSM, root-cause fusion."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from antar import clock
from antar.detect.changepoint import ChangepointDetector, SegmentTracker
from antar.detect.mandate_fsm import (
    IllegalTransition,
    MandateMachine,
    MandateRegistry,
)
from antar.detect.root_cause import ACTION_COST, RootCauseAnalyser, action_cost, unknown_rate
from antar.detect.taxonomy import (
    AMBIGUOUS_REASONS,
    RESOLVED_REASONS,
    candidates_for,
    classify,
    is_ambiguous,
)
from antar.ids import event_id as make_event_id
from antar.signals.downtime import DowntimeRegistry
from antar.signals.schemas import (
    AtRiskEvent,
    DowntimeSeverity,
    DowntimeStatus,
    DowntimeWindow,
    FailureClass,
    InterventionClass,
    LossClass,
    MandateState,
    MerchantCategory,
    PaymentMethod,
    SegmentHealth,
)

NOW = datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST)


def event(
    *,
    reason: str = "insufficient_funds",
    amount_paise: int = 49900,
    category: MerchantCategory = MerchantCategory.OTT_SUBSCRIPTION,
    issuer: str = "HDFC",
    method: PaymentMethod = PaymentMethod.UPI_AUTOPAY,
    subscription_id: str | None = "sub_1",
    at: datetime = NOW,
) -> AtRiskEvent:
    from antar.signals.razorpay_errors import BY_REASON

    entry = BY_REASON[reason]
    return AtRiskEvent(
        event_id=make_event_id("mer_1", "cust_1", subscription_id, at),
        merchant_id="mer_1",
        customer_id="cust_1",
        loss_class=LossClass.MANDATE_FAILURE,
        subscription_id=subscription_id,
        cycle_number=2,
        amount_paise=amount_paise,
        merchant_category=category,
        method=method,
        issuer=issuer,
        occurred_at=at,
        error_code=entry.code,
        error_reason=entry.reason,
        error_source=entry.source,
        error_step=entry.step,
        error_description=entry.description,
    )


# ------------------------------------------------------------------ taxonomy


def test_unambiguous_reasons_resolve():
    assert classify(event(reason="insufficient_funds")).failure_class is (
        FailureClass.INSUFFICIENT_FUNDS
    )
    assert classify(event(reason="card_expired")).failure_class is FailureClass.TECHNICAL_DECLINE
    assert classify(event(reason="mandate_revoked")).failure_class is FailureClass.MANDATE_REVOKED
    assert classify(event(reason="issuer_down")).failure_class is FailureClass.ISSUER_DOWN


def test_ambiguous_reasons_return_unknown_rather_than_guessing():
    for reason in ("gateway_technical_error", "declined_by_issuer", "payment_failed"):
        verdict = classify(event(reason=reason))
        assert verdict.failure_class is FailureClass.UNKNOWN, reason
        assert verdict.confidence == 0.0
        assert verdict.rationale, "an UNKNOWN must explain itself"


def test_missing_error_reason_is_unknown_not_a_crash():
    bare = event().model_copy(update={"error_reason": None})
    assert classify(bare).failure_class is FailureClass.UNKNOWN


def test_unrecognised_reason_says_whether_it_is_documented():
    unknown_code = event().model_copy(update={"error_reason": "quantum_decoherence"})
    verdict = classify(unknown_code)
    assert verdict.failure_class is FailureClass.UNKNOWN
    assert "not in the documented" in verdict.rationale


def test_candidates_constrain_the_classifier():
    assert set(candidates_for(event(reason="gateway_technical_error"))) == {
        FailureClass.ISSUER_DOWN,
        FailureClass.TECHNICAL_DECLINE,
    }
    assert set(candidates_for(event(reason="declined_by_issuer"))) == {
        FailureClass.INSUFFICIENT_FUNDS,
        FailureClass.RISK_DECLINE,
    }


def test_ambiguous_and_resolved_sets_are_disjoint_and_complete():
    assert not (AMBIGUOUS_REASONS & RESOLVED_REASONS)
    assert is_ambiguous(event(reason="payment_failed"))
    assert not is_ambiguous(event(reason="card_expired"))


# --------------------------------------------------------------- changepoint


def test_tracker_stays_healthy_through_ordinary_noise():
    """A 25% failure rate produces runs of failures. They are not outages."""
    import random

    rng = random.Random(11)
    tracker = SegmentTracker("HDFC:CARD", degrading_threshold=5.0, degraded_threshold=8.0)
    at = NOW
    alarms = 0
    for _ in range(600):
        at += timedelta(minutes=30)
        state = tracker.observe(rng.random() > 0.25, at)
        alarms += int(state in (SegmentHealth.DEGRADING, SegmentHealth.DEGRADED))
    assert alarms / 600 < 0.25, f"false-alarm rate {alarms / 600:.2%} is too high for noise"


def test_tracker_detects_a_sustained_collapse():
    tracker = SegmentTracker("HDFC:CARD", degrading_threshold=5.0, degraded_threshold=8.0)
    at = NOW
    for index in range(200):  # healthy baseline, ~25% failures
        at += timedelta(minutes=30)
        tracker.observe(index % 4 != 0, at)
    assert tracker.state is SegmentHealth.HEALTHY

    for _ in range(20):  # the rail falls over
        at += timedelta(minutes=5)
        tracker.observe(False, at)
    assert tracker.state is SegmentHealth.DEGRADED


def test_tracker_recovers_but_not_instantly():
    tracker = SegmentTracker("HDFC:CARD", degrading_threshold=5.0, degraded_threshold=8.0)
    at = NOW
    for index in range(200):
        at += timedelta(minutes=30)
        tracker.observe(index % 4 != 0, at)
    for _ in range(20):
        at += timedelta(minutes=5)
        tracker.observe(False, at)
    assert tracker.state is SegmentHealth.DEGRADED

    # One good attempt does not clear an outage: the accumulated deviation has to be
    # worked off first.
    at += timedelta(minutes=5)
    tracker.observe(True, at)
    assert tracker.state is SegmentHealth.DEGRADED, "one success must not clear a degradation"

    seen: list[SegmentHealth] = []
    for _ in range(80):
        at += timedelta(minutes=5)
        seen.append(tracker.observe(True, at))
    assert tracker.state is SegmentHealth.HEALTHY

    # ...and it went through RECOVERING rather than snapping straight back.
    assert SegmentHealth.RECOVERING in seen
    first_healthy = seen.index(SegmentHealth.HEALTHY)
    assert SegmentHealth.RECOVERING in seen[:first_healthy]


def test_cusum_is_standardised():
    """docs/POSTMORTEM.md D8.

    The allowance and the thresholds are in units of sigma, so the increment must be
    too. If it is not, two consecutive failures declare an outage.
    """
    tracker = SegmentTracker("X:CARD", min_observations=4, degrading_threshold=5.0)
    at = NOW
    for _ in range(4):
        at += timedelta(minutes=1)
        tracker.observe(True, at)
    for _ in range(2):
        at += timedelta(minutes=1)
        tracker.observe(False, at)
    assert tracker.state is SegmentHealth.HEALTHY, (
        "two consecutive failures must not declare a segment degrading"
    )


def test_health_at_answers_historical_questions():
    detector = ChangepointDetector(degrading_threshold=5.0, degraded_threshold=8.0)
    at = NOW
    stamps = []
    for index in range(200):
        at += timedelta(minutes=30)
        detector.observe("HDFC:CARD", success=index % 4 != 0, at=at)
        stamps.append(at)
    early = stamps[10]
    for _ in range(20):
        at += timedelta(minutes=5)
        detector.observe("HDFC:CARD", success=False, at=at)

    assert detector.health_at("HDFC:CARD", early) is SegmentHealth.HEALTHY
    assert detector.health_at("HDFC:CARD", at) is SegmentHealth.DEGRADED
    # Before any observation, we had no evidence of trouble.
    assert detector.health_at("HDFC:CARD", NOW - timedelta(days=1)) is SegmentHealth.HEALTHY
    assert detector.health_at("NOSUCH:CARD", at) is SegmentHealth.HEALTHY


# ------------------------------------------------------------- mandate FSM


def test_legal_transitions_are_allowed():
    machine = MandateMachine("sub_1")
    assert machine.apply(MandateState.PAUSED, at=NOW) is MandateState.PAUSED
    assert machine.apply(MandateState.ACTIVE, at=NOW) is MandateState.ACTIVE
    assert machine.apply(MandateState.HALTED, at=NOW) is MandateState.HALTED
    assert machine.apply(MandateState.REVOKED, at=NOW) is MandateState.REVOKED


def test_terminal_states_absorb_rather_than_raise():
    """A late webhook for a cancelled subscription is normal, not a programming error."""
    machine = MandateMachine("sub_1")
    machine.apply(MandateState.REVOKED, at=NOW)
    assert machine.apply(MandateState.ACTIVE, at=NOW, cause="late redelivery") is (
        MandateState.REVOKED
    )
    assert len(machine.ignored) == 1


def test_illegal_transition_raises():
    machine = MandateMachine("sub_1")
    machine.apply(MandateState.PAUSED, at=NOW)
    with pytest.raises(IllegalTransition):
        machine.apply(MandateState.HALTED, at=NOW)


def test_a_paused_mandate_is_not_a_retry_candidate():
    """RBI-EM-06. Debiting a paused mandate is a debit the customer forbade."""
    machine = MandateMachine("sub_1")
    machine.apply(MandateState.PAUSED, at=NOW)
    assert not machine.can_retry
    assert "paused" in (machine.why_not_retryable() or "").lower()


def test_out_of_order_lifecycle_reaches_the_right_terminal_state():
    registry = MandateRegistry()
    registry.apply_stream(
        [
            ("sub_1", "subscription.cancelled", NOW + timedelta(hours=2)),
            ("sub_1", "subscription.charged", NOW),  # arrives second, happened first
        ]
    )
    assert registry.state_of("sub_1") is MandateState.REVOKED


def test_state_at_uses_only_what_was_knowable_then():
    """docs/POSTMORTEM.md D10.

    The cancellation webhook is timestamped at the moment of cancellation, which is
    the same moment the debit failed. Asking for the *current* state while diagnosing
    that failure would let the detector see a cancellation it could not have known
    about, and turn recall into a measurement of hindsight.
    """
    registry = MandateRegistry()
    cancelled_at = NOW + timedelta(hours=1)
    registry.apply_stream([("sub_1", "subscription.cancelled", cancelled_at)])

    assert registry.state_at("sub_1", NOW) is MandateState.ACTIVE
    assert registry.state_at("sub_1", cancelled_at) is MandateState.ACTIVE
    assert registry.state_at("sub_1", cancelled_at + timedelta(seconds=1)) is MandateState.REVOKED
    assert registry.state_of("sub_1") is MandateState.REVOKED


def test_checkout_abandonment_has_no_mandate_in_the_way():
    assert MandateRegistry().state_at(None, NOW) is MandateState.ACTIVE


def test_illegal_transitions_are_recorded_not_fatal():
    registry = MandateRegistry()
    registry.get("sub_1").apply(MandateState.PAUSED, at=NOW)
    registry.apply_stream([("sub_1", "subscription.halted", NOW + timedelta(hours=1))])
    assert len(registry.illegal_attempts) == 1
    assert registry.summary()["illegal_transitions_rejected"] == 1


# ------------------------------------------------------------- root cause


def analyser(**kwargs) -> RootCauseAnalyser:
    defaults = {
        "downtime": DowntimeRegistry(),
        "mandates": MandateRegistry(),
        "classifier": None,
        "changepoint": None,
    }
    return RootCauseAnalyser(**{**defaults, **kwargs})


def test_issuer_down_recommends_wait():
    diagnosis = analyser().diagnose(event(reason="issuer_down"))
    assert diagnosis.failure_class is FailureClass.ISSUER_DOWN
    assert diagnosis.recommended_class is InterventionClass.WAIT


def test_insufficient_funds_recommends_reschedule():
    diagnosis = analyser().diagnose(event(reason="insufficient_funds"))
    assert diagnosis.recommended_class is InterventionClass.RESCHEDULE


def test_revoked_mandate_recommends_terminate():
    diagnosis = analyser().diagnose(event(reason="mandate_revoked"))
    assert diagnosis.recommended_class is InterventionClass.TERMINATE


def test_downtime_overlap_resolves_an_ambiguous_code():
    window = DowntimeWindow(
        downtime_id="down_1",
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        severity=DowntimeSeverity.HIGH,
        status=DowntimeStatus.STARTED,
        begin=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=1),
    )
    diagnosis = analyser(downtime=DowntimeRegistry([window])).diagnose(
        event(reason="gateway_technical_error")
    )
    assert diagnosis.failure_class is FailureClass.ISSUER_DOWN
    assert diagnosis.downtime_overlap is not None
    assert any(item.code == "DOWNTIME_OVERLAP" for item in diagnosis.evidence)


def test_a_high_severity_outage_overrides_an_unambiguous_technical_decline():
    """A card decline in the middle of a major outage is probably the outage."""
    window = DowntimeWindow(
        downtime_id="down_1",
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        severity=DowntimeSeverity.HIGH,
        status=DowntimeStatus.STARTED,
        begin=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=1),
    )
    diagnosis = analyser(downtime=DowntimeRegistry([window])).diagnose(
        event(reason="card_blocked")
    )
    assert diagnosis.failure_class is FailureClass.ISSUER_DOWN
    assert any(item.code == "DOWNTIME_OVERRIDE" for item in diagnosis.evidence)


def test_a_low_severity_outage_does_not_override():
    window = DowntimeWindow(
        downtime_id="down_1",
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        severity=DowntimeSeverity.LOW,
        status=DowntimeStatus.STARTED,
        begin=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=1),
    )
    diagnosis = analyser(downtime=DowntimeRegistry([window])).diagnose(
        event(reason="card_blocked")
    )
    assert diagnosis.failure_class is FailureClass.TECHNICAL_DECLINE


def test_afa_boundary_resolves_an_authentication_code():
    diagnosis = analyser().diagnose(
        event(reason="incorrect_otp", amount_paise=2_500_000)  # Rs 25,000 > Rs 15,000
    )
    assert diagnosis.failure_class is FailureClass.AFA_REQUIRED
    assert diagnosis.afa_required


def test_the_high_ceiling_categories_do_not_trip_afa_at_the_general_threshold():
    """RBI-EM-04: insurance, mutual funds and credit-card bills get Rs 1,00,000."""
    diagnosis = analyser().diagnose(
        event(
            reason="incorrect_otp",
            amount_paise=2_500_000,
            category=MerchantCategory.INSURANCE,
        )
    )
    assert diagnosis.failure_class is not FailureClass.AFA_REQUIRED


def test_unresolvable_events_wait_rather_than_guess():
    diagnosis = analyser().diagnose(event(reason="payment_failed"))
    assert diagnosis.failure_class is FailureClass.UNKNOWN
    assert diagnosis.recommended_class is InterventionClass.WAIT
    assert any(item.code == "UNRESOLVED" for item in diagnosis.evidence)


def test_every_diagnosis_carries_evidence():
    for reason in ("insufficient_funds", "payment_failed", "gateway_technical_error"):
        diagnosis = analyser().diagnose(event(reason=reason))
        assert diagnosis.evidence, f"{reason} produced a diagnosis with no evidence"
        assert all(item.detail for item in diagnosis.evidence)


def test_unknown_rate_is_computable():
    diagnoses = [analyser().diagnose(event(reason=r)) for r in ("payment_failed", "card_expired")]
    assert unknown_rate(diagnoses) == 0.5
    assert unknown_rate([]) == 0.0


# ------------------------------------------------------------ action costs


def test_correct_actions_are_free():
    assert action_cost(InterventionClass.WAIT, FailureClass.ISSUER_DOWN) == 0.0
    assert action_cost(InterventionClass.RESCHEDULE, FailureClass.INSUFFICIENT_FUNDS) == 0.0
    assert action_cost(InterventionClass.CONTACT, FailureClass.TECHNICAL_DECLINE) == 0.0
    assert action_cost(InterventionClass.TERMINATE, FailureClass.MANDATE_REVOKED) == 0.0


def test_terminating_a_recoverable_customer_is_the_most_expensive_error():
    """The error nobody notices, because the counterfactual never shows up."""
    terminate = action_cost(InterventionClass.TERMINATE, FailureClass.INSUFFICIENT_FUNDS)
    assert terminate == 1.0
    for action in (InterventionClass.WAIT, InterventionClass.CONTACT, InterventionClass.RESCHEDULE):
        assert action_cost(action, FailureClass.INSUFFICIENT_FUNDS) < terminate


def test_contacting_during_an_outage_is_priced():
    """It burns a contact slot and delivers an RBI-EM-02 opt-out prompt to someone
    who did nothing wrong."""
    assert action_cost(InterventionClass.CONTACT, FailureClass.ISSUER_DOWN) > 0


def test_an_undemonstrable_truth_is_not_charged():
    assert action_cost(InterventionClass.CONTACT, FailureClass.UNKNOWN) == 0.0


def test_the_cost_matrix_covers_every_action_and_cause():
    for action in InterventionClass:
        for cause in FailureClass:
            if cause is FailureClass.UNKNOWN:
                continue
            assert (action, cause) in ACTION_COST, f"unpriced: {action.value} x {cause.value}"
