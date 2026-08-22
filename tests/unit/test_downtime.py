"""Downtime registry and poller.

The question this module has to answer correctly is "was the bank down when this
debit failed?", because getting it wrong means contacting a customer who could not
have paid - burning a TRAI contact slot and an RBI-EM-01 notification, each of which
carries an opt-out, on a failure that was never theirs.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

import httpx

from antar.signals.downtime import (
    DowntimePoller,
    DowntimeRegistry,
    parse_downtime_entity,
)
from antar.signals.razorpay_client import RazorpayClient
from antar.signals.schemas import (
    DowntimeSeverity,
    DowntimeStatus,
    DowntimeWindow,
    PaymentMethod,
)
from tests.conftest import FIXTURE_ROOT, FROZEN_NOW


def window(
    did: str = "down_1",
    *,
    method: PaymentMethod = PaymentMethod.CARD,
    issuer: str | None = "HDFC",
    severity: DowntimeSeverity = DowntimeSeverity.HIGH,
    status: DowntimeStatus = DowntimeStatus.STARTED,
    begin_offset_hours: float = -1.0,
    end_offset_hours: float | None = None,
) -> DowntimeWindow:
    return DowntimeWindow(
        downtime_id=did,
        method=method,
        issuer=issuer,
        severity=severity,
        status=status,
        begin=FROZEN_NOW + timedelta(hours=begin_offset_hours),
        end=None if end_offset_hours is None else FROZEN_NOW + timedelta(hours=end_offset_hours),
    )


def test_open_window_covers_now():
    assert window().covers(FROZEN_NOW)


def test_closed_window_does_not_cover_a_later_instant():
    closed = window(end_offset_hours=-0.5)
    assert not closed.covers(FROZEN_NOW)
    assert closed.covers(FROZEN_NOW - timedelta(minutes=45))


def test_tolerance_catches_a_failure_that_surfaced_after_the_window_closed():
    """A debit queued during an outage can fail minutes after it lifts.

    Scoring that as a customer decline is exactly the misattribution L2 exists to
    prevent, so the overlap check carries a tolerance.
    """
    closed = window(end_offset_hours=-0.25)  # ended 15 minutes ago
    assert not closed.covers(FROZEN_NOW)
    assert closed.covers(FROZEN_NOW, tolerance_minutes=30)


def test_window_only_affects_its_own_method_and_issuer():
    w = window(method=PaymentMethod.CARD, issuer="HDFC")
    assert w.affects(PaymentMethod.CARD, "HDFC")
    assert not w.affects(PaymentMethod.UPI_AUTOPAY, "HDFC")
    assert not w.affects(PaymentMethod.CARD, "ICICI")
    # A window with no issuer is method-wide.
    assert window(issuer=None).affects(PaymentMethod.CARD, "ICICI")


def test_registry_returns_the_most_severe_overlap():
    registry = DowntimeRegistry(
        [
            window("low", severity=DowntimeSeverity.LOW),
            window("high", severity=DowntimeSeverity.HIGH),
            window("medium", severity=DowntimeSeverity.MEDIUM),
        ]
    )
    hit = registry.overlap_for(FROZEN_NOW, PaymentMethod.CARD, "HDFC")
    assert hit is not None and hit.downtime_id == "high"


def test_registry_returns_none_for_an_unaffected_segment():
    registry = DowntimeRegistry([window(issuer="HDFC")])
    assert registry.overlap_for(FROZEN_NOW, PaymentMethod.CARD, "SBI") is None


def test_resolved_status_is_not_reverted_by_a_late_started_redelivery():
    """Razorpay redelivers. A late `started` must not resurrect a finished outage."""
    registry = DowntimeRegistry()
    registry.upsert(window("down_1", status=DowntimeStatus.STARTED))
    registry.upsert(window("down_1", status=DowntimeStatus.RESOLVED, end_offset_hours=-0.1))
    registry.upsert(window("down_1", status=DowntimeStatus.STARTED))  # late redelivery
    stored = next(iter(registry))
    assert stored.status is DowntimeStatus.RESOLVED


def test_resolved_window_can_still_be_updated_by_another_resolved_payload():
    registry = DowntimeRegistry()
    registry.upsert(window("down_1", status=DowntimeStatus.RESOLVED, end_offset_hours=-1.0))
    registry.upsert(
        window("down_1", status=DowntimeStatus.RESOLVED, end_offset_hours=-0.5,
               severity=DowntimeSeverity.LOW)
    )
    assert next(iter(registry)).severity is DowntimeSeverity.LOW


def test_prune_drops_only_old_resolved_windows():
    registry = DowntimeRegistry(
        [
            window("open", status=DowntimeStatus.STARTED),
            window("recent", status=DowntimeStatus.RESOLVED, end_offset_hours=-2),
            window("stale", status=DowntimeStatus.RESOLVED, end_offset_hours=-24 * 10),
        ]
    )
    assert registry.prune_resolved(older_than=timedelta(days=7)) == 1
    assert {w.downtime_id for w in registry} == {"open", "recent"}


def test_parse_downtime_entity_from_the_api_shape():
    payload = json.loads((FIXTURE_ROOT / "downtimes_list.json").read_text(encoding="utf-8"))
    windows = [parse_downtime_entity(item) for item in payload["items"]]
    assert len(windows) == 2
    card = next(w for w in windows if w.method is PaymentMethod.CARD)
    assert card.severity is DowntimeSeverity.HIGH
    assert card.status is DowntimeStatus.STARTED
    assert card.issuer == "HDFC"
    assert card.end is None
    upi = next(w for w in windows if w.method is PaymentMethod.UPI_AUTOPAY)
    assert upi.status is DowntimeStatus.RESOLVED and upi.end is not None


def test_unknown_severity_and_status_fall_back_rather_than_crash():
    """A new enum value from Razorpay must not take down the batch."""
    parsed = parse_downtime_entity(
        {"id": "d", "method": "card", "severity": "catastrophic", "status": "smouldering"}
    )
    assert parsed.severity is DowntimeSeverity.MEDIUM
    assert parsed.status is DowntimeStatus.STARTED


def test_poller_folds_the_api_response_into_the_registry():
    payload = json.loads((FIXTURE_ROOT / "downtimes_list.json").read_text(encoding="utf-8"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    client = RazorpayClient(
        key_id="rzp_test_x", key_secret="y", transport=transport, rng=random.Random(0)
    )
    poller = DowntimePoller(client)
    assert len(poller.poll()) == 2
    assert len(poller.registry) == 2
    # Polling again is idempotent - the same ids upsert rather than accumulate.
    poller.poll()
    assert len(poller.registry) == 2
