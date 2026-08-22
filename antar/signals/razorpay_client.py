"""A typed Razorpay client with the failure behaviour a money system needs.

Four things this wrapper adds over raw HTTP, each of which exists because of a
specific line in the chaos suite (PLAN.md section 10):

  * **Retry with exponential backoff and jitter.** A 500-then-200 sequence must not
    surface an error, and a fleet of workers retrying in lockstep is a self-inflicted
    outage. Jitter is not decoration.
  * **Circuit breaker.** When the API is down, the correct behaviour for a recovery
    batch is to degrade to WAIT, not to burn every attempt into a closed door.
  * **Idempotency keys.** Every mutating call carries one. A retried charge that
    creates a second debit is the worst bug this system can have.
  * **Structured error mapping.** Razorpay's error envelope becomes a typed exception
    carrying `code`, `reason`, `source`, `step`, so the detection layer never has to
    parse a string.

Nothing here calls `datetime.now()`; the clock comes from `antar.clock`.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import httpx

from antar import clock
from antar.config import secret

BASE_URL = "https://api.razorpay.com/v1"

# Methods that create or change state and therefore need an idempotency key.
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RazorpayError(Exception):
    """A structured failure from the Razorpay API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        source: str | None = None,
        step: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.reason = reason
        self.source = source
        self.step = step
        self.payload = payload or {}

    @property
    def retryable(self) -> bool:
        if self.status_code is None:
            return True  # transport-level failure
        return self.status_code == 429 or self.status_code >= 500

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RazorpayError(status={self.status_code}, code={self.code!r}, "
            f"reason={self.reason!r}, source={self.source!r}, step={self.step!r})"
        )


class CircuitOpenError(RazorpayError):
    """Raised when the breaker is open. Not a payment failure - an availability one."""

    def __init__(self, message: str, *, opens_until: float) -> None:
        super().__init__(message)
        self.opens_until = opens_until

    @property
    def retryable(self) -> bool:
        return False


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreaker:
    """Trips after `failure_threshold` consecutive retryable failures.

    Uses a monotonic counter rather than wall time so that a skewed worker clock
    cannot hold the breaker open forever, or slam it shut early.
    """

    failure_threshold: int = 5
    reset_after_seconds: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    # Assigned per instance so a test can substitute a scripted clock without
    # touching the class. A plain class-level default would be shared state.
    _monotonic: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._monotonic = time.monotonic

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._monotonic() - self._opened_at >= self.reset_after_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def before_request(self) -> None:
        if self.state is BreakerState.OPEN:
            assert self._opened_at is not None
            remaining = self.reset_after_seconds - (self._monotonic() - self._opened_at)
            raise CircuitOpenError(
                f"razorpay circuit breaker open for another {remaining:.1f}s",
                opens_until=self._opened_at + self.reset_after_seconds,
            )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._monotonic()

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    jitter: float = 0.5

    def delay_for(self, attempt: int, rng: random.Random) -> float:
        """Full-jitter exponential backoff.

        attempt is 1-based. `jitter=0` gives deterministic backoff, which the tests
        use so that a chaos case does not depend on a coin flip.
        """
        raw = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        if self.jitter <= 0:
            return raw
        return raw * (1.0 - self.jitter + self.jitter * rng.random() * 2.0)


class RazorpayClient:
    """Typed wrapper over the endpoints Antar consumes.

    Every mutating call requires an explicit `idempotency_key`. There is no default
    and no auto-generation: the caller is the only party that knows whether this is a
    fresh attempt or a replay, and guessing on their behalf is how double charges
    happen.
    """

    def __init__(
        self,
        *,
        key_id: str | None = None,
        key_secret: str | None = None,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Any = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.key_id = key_id or secret("RAZORPAY_KEY_ID") or ""
        self.key_secret = key_secret or secret("RAZORPAY_KEY_SECRET") or ""
        self.base_url = base_url.rstrip("/")
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker()
        self._sleep = sleep
        self._rng = rng or random.Random(0)
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(self.key_id, self.key_secret) if self.key_id else None,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "antar/0.1 (razorpay-buildathon)"},
        )
        self.call_log: list[tuple[str, str, str | None]] = []

    # ------------------------------------------------------------------ core

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RazorpayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if method in MUTATING and not idempotency_key:
            raise ValueError(
                f"{method} {path} is a mutating call and requires an idempotency key; "
                "see antar.ids.idempotency_key"
            )

        headers: dict[str, str] = {}
        if idempotency_key:
            headers["X-Razorpay-Idempotency-Key"] = idempotency_key

        self.call_log.append((method, path, idempotency_key))
        last_error: RazorpayError | None = None

        for attempt in range(1, self.retry.max_attempts + 1):
            self.breaker.before_request()
            try:
                response = self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = RazorpayError(f"transport failure: {exc}")
            else:
                if response.status_code < 400:
                    self.breaker.record_success()
                    return self._decode(response)
                last_error = self._to_error(response)

            if not last_error.retryable or attempt == self.retry.max_attempts:
                self.breaker.record_failure()
                raise last_error

            self.breaker.record_failure()
            self._sleep(self._backoff(attempt, last_error))

        raise last_error  # pragma: no cover - loop always returns or raises

    def _backoff(self, attempt: int, error: RazorpayError) -> float:
        """Honour Retry-After on a 429 rather than guessing over the top of it."""
        retry_after = error.payload.get("_retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return float(retry_after)
        return self.retry.delay_for(attempt, self._rng)

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise RazorpayError(
                f"non-JSON response from {response.request.url}",
                status_code=response.status_code,
            ) from exc
        return body if isinstance(body, dict) else {"items": body}

    @staticmethod
    def _to_error(response: httpx.Response) -> RazorpayError:
        payload: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body.get("error", body)
        except ValueError:
            payload = {"description": response.text[:500]}

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            # Razorpay may send an HTTP-date rather than seconds; a value we cannot
            # parse simply falls back to our own backoff rather than failing.
            with suppress(ValueError):
                payload = {**payload, "_retry_after": float(retry_after)}

        return RazorpayError(
            payload.get("description") or f"HTTP {response.status_code}",
            status_code=response.status_code,
            code=payload.get("code"),
            reason=payload.get("reason"),
            source=payload.get("source"),
            step=payload.get("step"),
            payload=payload,
        )

    # ------------------------------------------------------------- endpoints

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self.request("GET", f"/payments/{payment_id}")

    def fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self.request("GET", f"/subscriptions/{subscription_id}")

    def fetch_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.request("GET", f"/invoices/{invoice_id}")

    def create_order(
        self, *, amount_paise: int, currency: str = "INR", notes: dict[str, Any], key: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/orders",
            json={"amount": amount_paise, "currency": currency, "notes": notes},
            idempotency_key=key,
        )

    def charge_mandate(
        self,
        *,
        token: str,
        customer_id: str,
        amount_paise: int,
        order_id: str,
        method: str,
        key: str,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Debit a registered mandate. The single most dangerous call in the system.

        It is only ever reached through `antar.act.executors.retry_charge`, which is
        only ever reached through the PolicyGate. There is no other caller.
        """
        return self.request(
            "POST",
            "/payments/create/recurring",
            json={
                "token": token,
                "customer_id": customer_id,
                "amount": amount_paise,
                "currency": currency,
                "order_id": order_id,
                "recurring": "1",
                "method": method,
            },
            idempotency_key=key,
        )

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        customer: dict[str, Any],
        description: str,
        key: str,
        expire_by: int | None = None,
        notify: dict[str, bool] | None = None,
        currency: str = "INR",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "description": description,
            "customer": customer,
            # Antar sends its own notifications through the gate; letting Razorpay
            # notify would put a message outside the audit ledger and outside the
            # TRAI contact budget.
            "notify": notify or {"sms": False, "email": False},
            "reminder_enable": False,
        }
        if expire_by is not None:
            body["expire_by"] = expire_by
        return self.request("POST", "/payment_links", json=body, idempotency_key=key)

    def cancel_subscription(
        self, subscription_id: str, *, key: str, cancel_at_cycle_end: bool = False
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            json={"cancel_at_cycle_end": int(cancel_at_cycle_end)},
            idempotency_key=key,
        )

    def create_refund(self, payment_id: str, *, amount_paise: int, key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/payments/{payment_id}/refund",
            json={"amount": amount_paise},
            idempotency_key=key,
        )

    def fetch_downtimes(self) -> dict[str, Any]:
        """Payment downtime, as reported by Razorpay's Downtime API."""
        return self.request("GET", "/payments/downtimes")


def verify_webhook_signature(body: bytes, signature: str, secret_key: str) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    The raw bytes matter: re-serialising the parsed JSON changes key order and
    whitespace, and the signature then fails for a legitimate payload. The receiver
    therefore keeps the original bytes all the way to this function.
    """
    if not secret_key or not signature:
        return False
    expected = hmac.new(secret_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def utc_epoch(dt: Any = None) -> int:
    """Razorpay timestamps are UNIX seconds. Sourced from the authoritative clock."""
    moment = dt or clock.now()
    return int(moment.timestamp())
