"""Downtime tracking.

The single most valuable thing this module does is let the detection layer answer
"was the bank down when this debit failed?" A failure during an issuer outage is not
the customer's failure. Contacting that customer is pure waste: they cannot pay, the
decline says nothing about their intent, and under RBI-EM-01 a retry costs a
24-hour notification that carries an opt-out. Separating ISSUER_DOWN from
INSUFFICIENT_FUNDS is the reason L2 exists.

Two sources are fused: the poller (`GET /payments/downtimes`, the current picture)
and the webhook stream (`payment.downtime.*`, the incremental one). They disagree
routinely - a resolved window lingers in one and not the other - so the registry
keeps the union and lets the most recent status for a given id win.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Any

from antar import clock
from antar.signals.razorpay_client import RazorpayClient
from antar.signals.schemas import (
    DowntimeSeverity,
    DowntimeStatus,
    DowntimeWindow,
    PaymentMethod,
)

METHOD_MAP: dict[str, PaymentMethod] = {
    "upi": PaymentMethod.UPI_AUTOPAY,
    "card": PaymentMethod.CARD,
    "emandate": PaymentMethod.ENACH,
    "nach": PaymentMethod.ENACH,
    "netbanking": PaymentMethod.ENACH,
}

# How much of a failure a window of each severity explains. Used by the detector to
# decide whether an outage is a sufficient explanation for this decline or merely a
# contributing one. Author-chosen; see docs/SIMULATOR_CARD.md section 4.5.
SEVERITY_WEIGHT: dict[DowntimeSeverity, float] = {
    DowntimeSeverity.LOW: 0.35,
    DowntimeSeverity.MEDIUM: 0.70,
    DowntimeSeverity.HIGH: 0.97,
}


class DowntimeRegistry:
    """Every downtime window we know about, queryable by (method, issuer, instant)."""

    def __init__(self, windows: Iterable[DowntimeWindow] = ()) -> None:
        self._by_id: dict[str, DowntimeWindow] = {}
        for window in windows:
            self.upsert(window)

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[DowntimeWindow]:
        return iter(self._by_id.values())

    def upsert(self, window: DowntimeWindow) -> DowntimeWindow:
        """Insert or update. A resolved window never reverts to started.

        Razorpay redelivers webhooks, so a `started` payload can arrive after the
        `resolved` one. Letting it win would make the registry claim an outage that
        has been over for an hour.
        """
        existing = self._by_id.get(window.downtime_id)
        already_resolved = existing is not None and existing.status is DowntimeStatus.RESOLVED
        if already_resolved and window.status is not DowntimeStatus.RESOLVED:
            assert existing is not None
            return existing
        self._by_id[window.downtime_id] = window
        return window

    def extend(self, windows: Iterable[DowntimeWindow]) -> None:
        for window in windows:
            self.upsert(window)

    def active_at(
        self,
        when: datetime,
        *,
        method: PaymentMethod | None = None,
        issuer: str | None = None,
        tolerance_minutes: int = 0,
    ) -> list[DowntimeWindow]:
        out = []
        for window in self._by_id.values():
            if not window.covers(when, tolerance_minutes=tolerance_minutes):
                continue
            if method is not None and not window.affects(method, issuer):
                continue
            out.append(window)
        return sorted(out, key=lambda w: SEVERITY_WEIGHT[w.severity], reverse=True)

    def overlap_for(
        self,
        when: datetime,
        method: PaymentMethod,
        issuer: str | None,
        *,
        tolerance_minutes: int = 30,
    ) -> DowntimeWindow | None:
        """The most severe window covering this attempt, if any.

        The tolerance matters. A debit that entered the queue while the issuer was
        down can surface as a failure a few minutes after the window closes, and
        scoring that as a customer decline is exactly the misattribution this layer
        exists to prevent. 30 minutes is the configured default
        (`detect.downtime_overlap_tolerance_minutes`).
        """
        candidates = self.active_at(
            when, method=method, issuer=issuer, tolerance_minutes=tolerance_minutes
        )
        return candidates[0] if candidates else None

    def prune_resolved(self, *, older_than: timedelta = timedelta(days=7)) -> int:
        cutoff = clock.now() - older_than
        stale = [
            did
            for did, window in self._by_id.items()
            if window.status is DowntimeStatus.RESOLVED
            and window.end is not None
            and window.end < cutoff
        ]
        for did in stale:
            del self._by_id[did]
        return len(stale)


class DowntimePoller:
    """Polls `GET /payments/downtimes` and folds the result into a registry.

    Polling exists alongside webhooks because a worker that starts up mid-outage has
    missed the `started` webhook and would otherwise believe the world is healthy.
    """

    def __init__(self, client: RazorpayClient, registry: DowntimeRegistry | None = None) -> None:
        self.client = client
        self.registry = registry or DowntimeRegistry()

    def poll(self) -> list[DowntimeWindow]:
        response = self.client.fetch_downtimes()
        windows = [parse_downtime_entity(item) for item in response.get("items", [])]
        self.registry.extend(windows)
        return windows


def parse_downtime_entity(entity: dict[str, Any]) -> DowntimeWindow:
    """Parse one Downtime API entity."""
    instrument = entity.get("instrument") if isinstance(entity.get("instrument"), dict) else {}
    severity_raw = str(entity.get("severity", "medium")).lower()
    status_raw = str(entity.get("status", "started")).lower()
    return DowntimeWindow(
        downtime_id=str(entity.get("id", "down_unknown")),
        method=METHOD_MAP.get(str(entity.get("method", "")).lower(), PaymentMethod.CARD),
        issuer=(instrument or {}).get("issuer") or entity.get("issuer"),
        psp=(instrument or {}).get("psp") or entity.get("psp"),
        severity=(
            DowntimeSeverity(severity_raw)
            if severity_raw in {s.value for s in DowntimeSeverity}
            else DowntimeSeverity.MEDIUM
        ),
        status=(
            DowntimeStatus(status_raw)
            if status_raw in {s.value for s in DowntimeStatus}
            else DowntimeStatus.STARTED
        ),
        begin=_epoch(entity.get("begin")) or clock.now(),
        end=_epoch(entity.get("end")),
        scheduled=str(entity.get("scheduled", "false")).lower() in {"true", "1"},
    )


def _epoch(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=clock.IST)
    except (TypeError, ValueError, OSError):
        return None
