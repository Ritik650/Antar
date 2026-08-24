"""Webhook ingestion: verify, deduplicate, tolerate disorder, persist raw.

Three properties this module guarantees, each proven by a chaos test:

  * **A payload with a bad signature is rejected and never persisted as an event.**
    It is still logged, because a stream of forged webhooks is itself a signal.
  * **The same event delivered three times produces exactly one stored event.**
    Razorpay retries on non-2xx and duplicates are normal operation, not an attack.
  * **Out-of-order delivery reaches the correct terminal state.** `subscription.
    cancelled` arriving before `payment.failed` must not resurrect a dead mandate,
    so ordering is decided by the payload's own `created_at`, never by arrival time.

The raw bytes are kept from the socket to the signature check. Re-serialising parsed
JSON changes key order and breaks HMAC on payloads that are perfectly valid.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from antar import clock
from antar.config import secret
from antar.ids import event_id as make_event_id
from antar.signals.razorpay_client import verify_webhook_signature
from antar.signals.schemas import (
    AtRiskEvent,
    DowntimeSeverity,
    DowntimeStatus,
    DowntimeWindow,
    LossClass,
    MandateState,
    MerchantCategory,
    PaymentMethod,
)

# Webhook event names Antar consumes. Anything else is acknowledged and dropped:
# a 200 with no action beats a 500 that makes Razorpay retry something we will
# never understand.
AT_RISK_EVENTS = frozenset({"payment.failed", "subscription.pending", "invoice.expired"})
RECOVERY_EVENTS = frozenset({"payment.captured", "subscription.charged", "order.paid"})
MANDATE_EVENTS = frozenset(
    {
        "subscription.halted",
        "subscription.cancelled",
        "subscription.paused",
        "subscription.resumed",
        "subscription.completed",
    }
)
DOWNTIME_EVENTS = frozenset(
    {"payment.downtime.started", "payment.downtime.updated", "payment.downtime.resolved"}
)
CONSUMED_EVENTS = AT_RISK_EVENTS | RECOVERY_EVENTS | MANDATE_EVENTS | DOWNTIME_EVENTS

METHOD_MAP: dict[str, PaymentMethod] = {
    "upi": PaymentMethod.UPI_AUTOPAY,
    "card": PaymentMethod.CARD,
    "emandate": PaymentMethod.ENACH,
    "nach": PaymentMethod.ENACH,
    "netbanking": PaymentMethod.ENACH,
}

SUBSCRIPTION_STATE_MAP: dict[str, MandateState] = {
    "created": MandateState.ACTIVE,
    "authenticated": MandateState.ACTIVE,
    "active": MandateState.ACTIVE,
    "pending": MandateState.ACTIVE,
    "halted": MandateState.HALTED,
    "paused": MandateState.PAUSED,
    "cancelled": MandateState.REVOKED,
    "expired": MandateState.COMPLETED,
    "completed": MandateState.COMPLETED,
}


class WebhookRejected(Exception):
    """Signature verification failed, or the envelope was unparseable."""


@dataclass(frozen=True)
class ReceivedWebhook:
    """The outcome of ingesting one delivery."""

    delivery_id: str
    event_name: str
    accepted: bool
    duplicate: bool
    reason: str | None = None
    at_risk_event: AtRiskEvent | None = None
    downtime: DowntimeWindow | None = None
    mandate_state: MandateState | None = None
    subscription_id: str | None = None
    recovery_paise: int | None = None
    occurred_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class EventStore(Protocol):
    """Whatever persists raw deliveries. Kept narrow so tests can substitute a dict."""

    def seen(self, delivery_id: str) -> bool: ...

    def record(self, delivery_id: str, event_name: str, payload: dict[str, Any]) -> None: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []

    def seen(self, delivery_id: str) -> bool:
        return delivery_id in self.deliveries

    def record(self, delivery_id: str, event_name: str, payload: dict[str, Any]) -> None:
        self.deliveries[delivery_id] = {"event": event_name, "payload": payload}
        self.order.append(delivery_id)

    def __len__(self) -> int:
        return len(self.deliveries)


class WebhookReceiver:
    """Turns raw HTTP deliveries into typed signals.

    `merchant_category_for` exists because the RBI-EM-04 AFA ceiling is per merchant
    category and the webhook payload does not carry it. In production this is a
    lookup against merchant settings; here it is an injected callable so the
    dependency is visible rather than hidden in a global.
    """

    def __init__(
        self,
        *,
        store: EventStore | None = None,
        webhook_secret: str | None = None,
        merchant_category_for: Any = None,
        require_signature: bool = True,
    ) -> None:
        # `is None`: an empty store is falsy (D21).
        self.store = InMemoryEventStore() if store is None else store
        self.secret = webhook_secret if webhook_secret is not None else secret("RAZORPAY_WEBHOOK_SECRET")
        self.require_signature = require_signature
        self._category_for = merchant_category_for or (lambda _mid: MerchantCategory.GENERAL)
        self.rejected: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ entry

    def receive(
        self,
        body: bytes,
        *,
        signature: str | None = None,
        delivery_id: str | None = None,
    ) -> ReceivedWebhook:
        if self.require_signature:
            if not self.secret:
                raise WebhookRejected("no webhook secret configured; refusing to trust payload")
            if not signature or not verify_webhook_signature(body, signature, self.secret):
                self.rejected.append((delivery_id or "unknown", "bad_signature"))
                raise WebhookRejected("signature verification failed")

        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            self.rejected.append((delivery_id or "unknown", "malformed_json"))
            raise WebhookRejected(f"malformed webhook body: {exc}") from exc

        return self.dispatch(envelope, delivery_id=delivery_id)

    def dispatch(
        self, envelope: dict[str, Any], *, delivery_id: str | None = None
    ) -> ReceivedWebhook:
        """Handle an already-verified envelope. The simulator enters here."""
        event_name = str(envelope.get("event", ""))
        # Razorpay supplies the delivery id in the X-Razorpay-Event-Id header. When
        # it is absent we derive a stable one from the payload so that dedupe still
        # works - a duplicate delivery of identical content is still a duplicate.
        did = delivery_id or envelope.get("id") or _content_delivery_id(envelope)

        if self.store.seen(did):
            return ReceivedWebhook(
                delivery_id=did, event_name=event_name, accepted=False, duplicate=True,
                reason="duplicate delivery", raw=envelope,
            )

        if event_name not in CONSUMED_EVENTS:
            # Recorded, so a replay of the stream is faithful, but not acted on.
            self.store.record(did, event_name, envelope)
            return ReceivedWebhook(
                delivery_id=did, event_name=event_name, accepted=False, duplicate=False,
                reason="event type not consumed", raw=envelope,
            )

        self.store.record(did, event_name, envelope)
        occurred = _created_at(envelope)

        if event_name in DOWNTIME_EVENTS:
            return ReceivedWebhook(
                delivery_id=did, event_name=event_name, accepted=True, duplicate=False,
                downtime=self.parse_downtime(envelope), occurred_at=occurred, raw=envelope,
            )

        if event_name in MANDATE_EVENTS:
            sub = _entity(envelope, "subscription")
            return ReceivedWebhook(
                delivery_id=did, event_name=event_name, accepted=True, duplicate=False,
                mandate_state=SUBSCRIPTION_STATE_MAP.get(
                    str(sub.get("status", "")), MandateState.ACTIVE
                ),
                subscription_id=sub.get("id"), occurred_at=occurred, raw=envelope,
            )

        if event_name in RECOVERY_EVENTS:
            payment = _entity(envelope, "payment") or _entity(envelope, "order")
            return ReceivedWebhook(
                delivery_id=did, event_name=event_name, accepted=True, duplicate=False,
                recovery_paise=int(payment.get("amount", 0) or 0),
                subscription_id=_subscription_id(envelope), occurred_at=occurred, raw=envelope,
            )

        return ReceivedWebhook(
            delivery_id=did, event_name=event_name, accepted=True, duplicate=False,
            at_risk_event=self.parse_at_risk(envelope), occurred_at=occurred, raw=envelope,
        )

    # ------------------------------------------------------------- parsers

    def parse_at_risk(self, envelope: dict[str, Any]) -> AtRiskEvent:
        payment = _entity(envelope, "payment")
        error = {
            "code": payment.get("error_code"),
            "reason": payment.get("error_reason"),
            "source": payment.get("error_source"),
            "step": payment.get("error_step"),
            "description": payment.get("error_description"),
        }
        notes = payment.get("notes") or {}
        merchant_id = str(notes.get("merchant_id") or envelope.get("account_id") or "unknown")
        customer_id = str(payment.get("customer_id") or notes.get("customer_id") or "unknown")
        subscription_id = _subscription_id(envelope)
        cycle = notes.get("cycle_number")
        occurred = _created_at(envelope)

        return AtRiskEvent(
            event_id=make_event_id(merchant_id, customer_id, subscription_id, cycle or occurred),
            merchant_id=merchant_id,
            customer_id=customer_id,
            loss_class=(
                LossClass.MANDATE_FAILURE if subscription_id else LossClass.CHECKOUT_ABANDON
            ),
            subscription_id=subscription_id,
            cycle_number=int(cycle) if cycle is not None else None,
            amount_paise=int(payment.get("amount", 0) or 0),
            merchant_category=self._category_for(merchant_id),
            method=METHOD_MAP.get(str(payment.get("method", "")).lower(), PaymentMethod.CARD),
            issuer=str(payment.get("bank") or payment.get("wallet") or notes.get("issuer") or "UNKNOWN"),
            occurred_at=occurred,
            error_code=error["code"],
            error_reason=error["reason"],
            error_source=error["source"],
            error_step=error["step"],
            error_description=error["description"],
            raw=envelope,
        )

    def parse_downtime(self, envelope: dict[str, Any]) -> DowntimeWindow:
        entity = _entity(envelope, "payment.downtime") or _entity(envelope, "downtime")
        status_raw = str(entity.get("status", "started")).lower()
        status = (
            DowntimeStatus(status_raw)
            if status_raw in {s.value for s in DowntimeStatus}
            else DowntimeStatus.STARTED
        )
        begin = _epoch(entity.get("begin")) or _created_at(envelope)
        end = _epoch(entity.get("end"))
        severity_raw = str(entity.get("severity", "medium")).lower()
        return DowntimeWindow(
            downtime_id=str(entity.get("id", "down_unknown")),
            method=METHOD_MAP.get(str(entity.get("method", "")).lower(), PaymentMethod.CARD),
            issuer=entity.get("instrument", {}).get("issuer") if isinstance(entity.get("instrument"), dict) else entity.get("issuer"),
            psp=entity.get("instrument", {}).get("psp") if isinstance(entity.get("instrument"), dict) else entity.get("psp"),
            severity=(
                DowntimeSeverity(severity_raw)
                if severity_raw in {s.value for s in DowntimeSeverity}
                else DowntimeSeverity.MEDIUM
            ),
            status=status,
            begin=begin,
            end=end,
            scheduled=str(entity.get("scheduled", "false")).lower() in {"true", "1"},
        )

    # --------------------------------------------------------------- helpers

    def replay(self, envelopes: Iterable[dict[str, Any]]) -> list[ReceivedWebhook]:
        """Ingest a stream, sorted by the payload's own timestamp.

        This is what makes out-of-order arrival harmless: arrival order is discarded
        in favour of the order the events actually happened in.
        """
        ordered = sorted(envelopes, key=lambda e: (_created_at(e), str(e.get("event", ""))))
        return [self.dispatch(envelope) for envelope in ordered]


def _entity(envelope: dict[str, Any], name: str) -> dict[str, Any]:
    payload = envelope.get("payload") or {}
    wrapper = payload.get(name) or {}
    entity = wrapper.get("entity") if isinstance(wrapper, dict) else None
    return entity if isinstance(entity, dict) else {}


def _subscription_id(envelope: dict[str, Any]) -> str | None:
    sub = _entity(envelope, "subscription")
    if sub.get("id"):
        return str(sub["id"])
    payment = _entity(envelope, "payment")
    notes = payment.get("notes") or {}
    value = notes.get("subscription_id") or payment.get("invoice_id")
    return str(value) if value else None


def _epoch(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=clock.IST)
    except (TypeError, ValueError, OSError):
        return None


def _created_at(envelope: dict[str, Any]) -> datetime:
    return _epoch(envelope.get("created_at")) or clock.now()


def _content_delivery_id(envelope: dict[str, Any]) -> str:
    from antar.ids import deterministic_id

    return deterministic_id("whk", json.dumps(envelope, sort_keys=True, default=str))
