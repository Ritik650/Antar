"""The client's failure behaviour, which is the only interesting part of it."""

from __future__ import annotations

import random

import httpx
import pytest

from antar.ids import idempotency_key
from antar.signals.razorpay_client import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    RazorpayClient,
    RazorpayError,
    RetryPolicy,
    verify_webhook_signature,
)


class Recorder:
    """A transport that replays a scripted sequence of responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )


def make_client(*responses: httpx.Response, **kwargs) -> tuple[RazorpayClient, Recorder, list[float]]:
    recorder = Recorder(*responses)
    slept: list[float] = []
    client = RazorpayClient(
        key_id="rzp_test_dummy",
        key_secret="dummy",
        transport=httpx.MockTransport(recorder.handle_request),
        sleep=slept.append,
        rng=random.Random(7),
        **kwargs,
    )
    return client, recorder, slept


def json_response(status: int, body: dict, **headers) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def test_success_returns_decoded_body():
    client, recorder, _ = make_client(json_response(200, {"id": "pay_1", "status": "captured"}))
    assert client.fetch_payment("pay_1")["id"] == "pay_1"
    assert len(recorder.requests) == 1


def test_500_then_200_succeeds_without_surfacing_an_error():
    """PLAN.md section 10, first row of the chaos table."""
    client, recorder, slept = make_client(
        json_response(500, {"error": {"description": "server error"}}),
        json_response(200, {"id": "pay_1"}),
    )
    assert client.fetch_payment("pay_1")["id"] == "pay_1"
    assert len(recorder.requests) == 2
    assert len(slept) == 1 and slept[0] > 0


def test_backoff_is_exponential_and_jittered():
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=100.0, jitter=0.5)
    rng = random.Random(1)
    delays = [policy.delay_for(attempt, rng) for attempt in range(1, 5)]
    # Each attempt's band is centred on 2^(n-1) and jitter keeps them apart.
    assert 0.5 <= delays[0] <= 1.5
    assert 1.0 <= delays[1] <= 3.0
    assert 2.0 <= delays[2] <= 6.0
    # Two draws at the same attempt differ: a fleet does not retry in lockstep.
    assert policy.delay_for(3, rng) != policy.delay_for(3, rng)


def test_backoff_is_deterministic_when_jitter_disabled():
    policy = RetryPolicy(base_delay_seconds=0.5, jitter=0.0)
    rng = random.Random(0)
    assert [policy.delay_for(n, rng) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]


def test_429_honours_retry_after_rather_than_guessing():
    client, _, slept = make_client(
        json_response(429, {"error": {"description": "rate limited"}}, **{"Retry-After": "2.5"}),
        json_response(200, {"id": "pay_1"}),
    )
    client.fetch_payment("pay_1")
    assert slept == [2.5]


def test_429_does_not_duplicate_a_charge():
    """A rate-limited charge retries with the same idempotency key, never a new one."""
    client, recorder, _ = make_client(
        json_response(429, {"error": {"description": "rate limited"}}),
        json_response(200, {"id": "pay_ok"}),
    )
    key = idempotency_key("dec_x", 1)
    client.charge_mandate(
        token="token_1", customer_id="cust_1", amount_paise=49900,
        order_id="order_1", method="upi", key=key,
    )
    keys = {r.headers.get("X-Razorpay-Idempotency-Key") for r in recorder.requests}
    assert keys == {key}, "a retry must reuse the key, or the customer is charged twice"


def test_client_errors_are_not_retried():
    client, recorder, _ = make_client(
        json_response(400, {"error": {"code": "BAD_REQUEST_ERROR", "reason": "invalid_vpa",
                                      "source": "customer", "step": "payment_initiation",
                                      "description": "invalid vpa"}}),
    )
    with pytest.raises(RazorpayError) as exc:
        client.fetch_payment("pay_1")
    assert exc.value.reason == "invalid_vpa"
    assert exc.value.source == "customer"
    assert exc.value.step == "payment_initiation"
    assert not exc.value.retryable
    assert len(recorder.requests) == 1


def test_mutating_call_without_idempotency_key_is_rejected_before_the_wire():
    client, recorder, _ = make_client(json_response(200, {}))
    with pytest.raises(ValueError, match="idempotency key"):
        client.request("POST", "/payment_links", json={"amount": 100}, idempotency_key=None)
    assert recorder.requests == []


def test_circuit_breaker_opens_after_repeated_failures():
    breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=30)
    client, recorder, _ = make_client(
        json_response(500, {"error": {"description": "down"}}),
        retry=RetryPolicy(max_attempts=1),
        breaker=breaker,
    )
    for _ in range(2):
        with pytest.raises(RazorpayError):
            client.fetch_payment("pay_1")

    assert breaker.state is BreakerState.OPEN
    calls_before = len(recorder.requests)
    with pytest.raises(CircuitOpenError):
        client.fetch_payment("pay_1")
    assert len(recorder.requests) == calls_before, "an open breaker must not reach the network"


def test_circuit_breaker_half_opens_after_the_reset_window():
    breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=10)
    ticks = iter([0.0, 0.0, 5.0, 11.0])
    breaker._monotonic = lambda: next(ticks)
    breaker.record_failure()  # opened at t=0
    assert breaker.state is BreakerState.OPEN  # t=0
    assert breaker.state is BreakerState.OPEN  # t=5, still inside the window
    assert breaker.state is BreakerState.HALF_OPEN  # t=11, window elapsed


def test_circuit_open_error_is_not_retryable():
    """WAIT is the safe degradation. Retrying into an open breaker is not."""
    assert not CircuitOpenError("open", opens_until=0.0).retryable


def test_transport_failure_is_retried_then_surfaced():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    slept: list[float] = []
    client = RazorpayClient(
        key_id="rzp_test_dummy", key_secret="d",
        transport=httpx.MockTransport(explode),
        retry=RetryPolicy(max_attempts=3, jitter=0.0),
        sleep=slept.append,
    )
    with pytest.raises(RazorpayError, match="transport failure"):
        client.fetch_payment("pay_1")
    assert len(slept) == 2


def test_non_json_response_becomes_a_structured_error():
    client, _, _ = make_client(httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(RazorpayError, match="non-JSON"):
        client.fetch_payment("pay_1")


def test_payment_link_never_asks_razorpay_to_notify():
    """Antar's messages go through the gate. A Razorpay-sent SMS would not."""
    client, recorder, _ = make_client(json_response(200, {"id": "plink_1"}))
    client.create_payment_link(
        amount_paise=49900,
        customer={"name": "A", "contact": "+919900000000"},
        description="dues",
        key=idempotency_key("dec_x", 1),
    )
    body = recorder.requests[0].read().decode()
    assert '"sms":false' in body and '"email":false' in body
    assert '"reminder_enable":false' in body


def test_webhook_signature_verification():
    body = b'{"event":"payment.failed"}'
    secret_key = "whsec_test"
    import hashlib
    import hmac

    good = hmac.new(secret_key.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, good, secret_key)
    assert not verify_webhook_signature(body, good, "wrong_secret")
    assert not verify_webhook_signature(b'{"event":"tampered"}', good, secret_key)
    assert not verify_webhook_signature(body, "", secret_key)
    assert not verify_webhook_signature(body, good, "")
