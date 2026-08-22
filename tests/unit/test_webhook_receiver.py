"""Ingestion: verification, dedupe, ordering, and parsing every consumed type."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from antar.signals.schemas import (
    FailureClass,
    LossClass,
    MandateState,
    MerchantCategory,
    PaymentMethod,
)
from antar.signals.webhook_receiver import (
    CONSUMED_EVENTS,
    InMemoryEventStore,
    WebhookReceiver,
    WebhookRejected,
)
from tests.conftest import all_webhook_fixtures, load_fixture

SECRET = "whsec_antar_test"


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def receiver() -> WebhookReceiver:
    return WebhookReceiver(store=InMemoryEventStore(), webhook_secret=SECRET)


@pytest.fixture
def open_receiver() -> WebhookReceiver:
    """Signature checking off - for tests about parsing, not about auth."""
    return WebhookReceiver(store=InMemoryEventStore(), require_signature=False)


# ------------------------------------------------------------------ signature


def test_valid_signature_is_accepted(receiver):
    body = json.dumps(load_fixture("payment.failed.insufficient_funds")).encode()
    result = receiver.receive(body, signature=sign(body), delivery_id="evt_1")
    assert result.accepted
    assert result.at_risk_event is not None


def test_bad_signature_is_rejected_and_not_persisted(receiver):
    """PLAN.md section 10: rejected, logged, not persisted as an event."""
    body = json.dumps(load_fixture("payment.failed.insufficient_funds")).encode()
    with pytest.raises(WebhookRejected, match="signature"):
        receiver.receive(body, signature="deadbeef", delivery_id="evt_1")
    assert len(receiver.store) == 0
    assert receiver.rejected == [("evt_1", "bad_signature")]


def test_tampered_body_fails_verification(receiver):
    original = json.dumps(load_fixture("payment.failed.insufficient_funds")).encode()
    signature = sign(original)
    tampered = original.replace(b'"amount": 49900', b'"amount": 4990000')
    with pytest.raises(WebhookRejected):
        receiver.receive(tampered, signature=signature, delivery_id="evt_1")


def test_missing_secret_refuses_to_trust_anything():
    strict = WebhookReceiver(store=InMemoryEventStore(), webhook_secret="")
    with pytest.raises(WebhookRejected, match="no webhook secret"):
        strict.receive(b"{}", signature="anything")


def test_malformed_json_is_rejected(receiver):
    body = b'{"event": "payment.failed", '
    with pytest.raises(WebhookRejected, match="malformed"):
        receiver.receive(body, signature=sign(body), delivery_id="evt_1")
    assert receiver.rejected[-1][1] == "malformed_json"


# --------------------------------------------------------------------- dedupe


def test_same_delivery_three_times_yields_one_event(receiver):
    """PLAN.md section 10: exactly one event, exactly one decision."""
    body = json.dumps(load_fixture("payment.failed.insufficient_funds")).encode()
    results = [
        receiver.receive(body, signature=sign(body), delivery_id="evt_dup") for _ in range(3)
    ]
    assert [r.accepted for r in results] == [True, False, False]
    assert [r.duplicate for r in results] == [False, True, True]
    assert len(receiver.store) == 1


def test_dedupe_falls_back_to_content_hash_when_no_delivery_id(open_receiver):
    envelope = load_fixture("payment.failed.issuer_down")
    first = open_receiver.dispatch(envelope)
    second = open_receiver.dispatch(dict(envelope))
    assert first.accepted and second.duplicate
    assert first.delivery_id == second.delivery_id


# ------------------------------------------------------------------- ordering


def test_out_of_order_delivery_reaches_the_correct_terminal_state(open_receiver):
    """A cancellation that arrives before the failure must still win.

    Arrival order is discarded in favour of the payload's own created_at, so a
    revoked mandate cannot be resurrected by a late-delivered earlier event.
    """
    failure = load_fixture("payment.failed.mandate_revoked")
    cancelled = load_fixture("subscription.cancelled")
    assert cancelled["created_at"] > failure["created_at"]

    results = open_receiver.replay([cancelled, failure])  # deliberately reversed
    ordered = [r.event_name for r in results]
    assert ordered == ["payment.failed", "subscription.cancelled"]
    assert results[-1].mandate_state is MandateState.REVOKED


# -------------------------------------------------------------------- parsing


def test_every_consumed_fixture_parses(open_receiver, fixture_names):
    """M1 acceptance: every consumed webhook type has a fixture and a parsing test."""
    seen: set[str] = set()
    for name in fixture_names:
        envelope = load_fixture(name)
        result = open_receiver.dispatch(envelope, delivery_id=name)
        assert result.event_name == envelope["event"], name
        if envelope["event"] in CONSUMED_EVENTS:
            assert result.accepted, f"{name} should have been accepted"
            seen.add(envelope["event"])
    # Nothing in CONSUMED_EVENTS may lack a fixture.
    assert CONSUMED_EVENTS - seen == set(), f"no fixture for: {sorted(CONSUMED_EVENTS - seen)}"


def test_at_risk_event_carries_the_error_quadruple(open_receiver):
    result = open_receiver.dispatch(load_fixture("payment.failed.insufficient_funds"))
    event = result.at_risk_event
    assert event is not None
    assert event.error_reason == "insufficient_funds"
    assert event.error_source == "customer"
    assert event.error_step == "payment_authorization"
    assert event.error_code == "BAD_REQUEST_ERROR"
    assert event.amount_paise == 49900
    assert event.method is PaymentMethod.UPI_AUTOPAY
    assert event.issuer == "HDFC"
    assert event.loss_class is LossClass.MANDATE_FAILURE
    assert event.cycle_number == 3
    assert event.segment_key == "HDFC:UPI_AUTOPAY"


def test_checkout_abandonment_is_the_second_loss_class(open_receiver):
    result = open_receiver.dispatch(load_fixture("invoice.expired"))
    event = result.at_risk_event
    assert event is not None
    assert event.loss_class is LossClass.CHECKOUT_ABANDON
    assert event.subscription_id is None


def test_merchant_category_is_injected_not_guessed():
    """RBI-EM-04's ceiling is category-dependent, and the payload does not carry it."""
    receiver = WebhookReceiver(
        store=InMemoryEventStore(),
        require_signature=False,
        merchant_category_for=lambda _mid: MerchantCategory.INSURANCE,
    )
    result = receiver.dispatch(load_fixture("payment.failed.afa_required"))
    assert result.at_risk_event is not None
    assert result.at_risk_event.merchant_category is MerchantCategory.INSURANCE


def test_downtime_events_parse_into_windows(open_receiver):
    started = open_receiver.dispatch(load_fixture("payment.downtime.started"))
    assert started.downtime is not None
    assert started.downtime.severity.value == "high"
    assert started.downtime.end is None

    resolved = open_receiver.dispatch(load_fixture("payment.downtime.resolved"))
    assert resolved.downtime is not None
    assert resolved.downtime.status.value == "resolved"
    assert resolved.downtime.end is not None


def test_mandate_lifecycle_maps_to_states(open_receiver):
    cases = {
        "subscription.halted": MandateState.HALTED,
        "subscription.cancelled": MandateState.REVOKED,
        "subscription.paused": MandateState.PAUSED,
    }
    for name, expected in cases.items():
        result = open_receiver.dispatch(load_fixture(name), delivery_id=name)
        assert result.mandate_state is expected, name


def test_recovery_event_reports_the_amount(open_receiver):
    result = open_receiver.dispatch(load_fixture("subscription.charged"))
    assert result.recovery_paise == 49900
    assert result.subscription_id == "sub_ANTAR00000001"


def test_unconsumed_event_is_recorded_but_not_acted_on(open_receiver):
    envelope = {"entity": "event", "event": "payout.processed", "payload": {}, "created_at": 1}
    result = open_receiver.dispatch(envelope, delivery_id="evt_payout")
    assert not result.accepted
    assert result.reason == "event type not consumed"
    assert len(open_receiver.store) == 1, "a replay of the stream must stay faithful"


def test_event_ids_are_stable_across_reruns(open_receiver):
    """Determinism (PLAN.md 9.3): the same payload always gets the same event id."""
    a = WebhookReceiver(store=InMemoryEventStore(), require_signature=False)
    b = WebhookReceiver(store=InMemoryEventStore(), require_signature=False)
    envelope = load_fixture("payment.failed.risk_decline")
    assert a.dispatch(envelope).at_risk_event.event_id == b.dispatch(envelope).at_risk_event.event_id


def test_fixture_error_reasons_are_all_documented():
    """No fixture may contain an error code Razorpay does not document."""
    from antar.signals.razorpay_errors import is_documented

    for name in all_webhook_fixtures():
        envelope = load_fixture(name)
        payment = (envelope.get("payload", {}).get("payment") or {}).get("entity") or {}
        reason = payment.get("error_reason")
        if reason:
            assert is_documented(reason), f"{name} uses undocumented reason {reason!r}"


def test_failure_class_enum_covers_every_documented_source():
    """A guard against silently dropping a class from the taxonomy later."""
    assert {c.value for c in FailureClass} >= {
        "INSUFFICIENT_FUNDS", "ISSUER_DOWN", "MANDATE_REVOKED",
        "AFA_REQUIRED", "TECHNICAL_DECLINE", "RISK_DECLINE", "UNKNOWN",
    }
