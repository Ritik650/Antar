"""The single authoritative clock.

SIMULATOR_CARD.md section 3.2 and PLAN.md section 10 (clock-skew chaos case) both
require that exactly one component knows what time it is. Every deadline in this
system is legally load-bearing:

  * RBI-EM-01 gives retries a 24-hour notification lead time (`C-LEAD`)
  * TRAI-01 confines contact to 09:00-21:00 recipient-local (`C-WINDOW`)

A worker whose wall clock has drifted must not be able to schedule a 23-hour
notification. So no module outside this one may call `datetime.now()`; the rule is
enforced by tests/unit/test_clock_discipline.py, which greps the package.

India observes no daylight saving, so IST is modelled as a fixed +05:30 offset
rather than a zoneinfo lookup. This removes a `tzdata` dependency on Windows and
is exactly correct for the jurisdiction. See ADR-0004.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


@runtime_checkable
class Clock(Protocol):
    """Anything that can say what time it is, in IST."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time, converted to IST. The production default."""

    def now(self) -> datetime:
        return datetime.now(tz=IST)

    def __repr__(self) -> str:  # pragma: no cover
        return "SystemClock()"


class FrozenClock:
    """A clock stuck at one instant. For tests and deterministic replay."""

    def __init__(self, at: datetime) -> None:
        self._at = _to_ist(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _to_ist(at)

    def advance(self, **delta: float) -> datetime:
        self._at = self._at + timedelta(**delta)
        return self._at

    def __repr__(self) -> str:  # pragma: no cover
        return f"FrozenClock({self._at.isoformat()})"


class SimulatedClock(FrozenClock):
    """The simulator's clock. Advances only when the event loop advances it.

    Identical mechanics to FrozenClock; the separate name documents intent at the
    call site and lets `assert_simulated()` distinguish the two.
    """

    def __init__(self, start: datetime, *, resolution_minutes: int = 1) -> None:
        super().__init__(start)
        self.resolution = timedelta(minutes=resolution_minutes)

    def tick(self, steps: int = 1) -> datetime:
        self._at = self._at + self.resolution * steps
        return self._at

    def __repr__(self) -> str:  # pragma: no cover
        return f"SimulatedClock({self._at.isoformat()})"


def _to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


class _ClockRegistry:
    """Thread-local-aware holder for the installed clock."""

    def __init__(self) -> None:
        self._default: Clock = SystemClock()
        self._local = threading.local()

    @property
    def current(self) -> Clock:
        return getattr(self._local, "clock", None) or self._default

    def install(self, clock: Clock) -> None:
        self._default = clock

    @contextmanager
    def scoped(self, clock: Clock) -> Iterator[Clock]:
        previous = getattr(self._local, "clock", None)
        self._local.clock = clock
        try:
            yield clock
        finally:
            self._local.clock = previous


_registry = _ClockRegistry()


def now() -> datetime:
    """The current time in IST, according to the authoritative clock."""
    return _registry.current.now()


def today() -> datetime:
    """Midnight IST at the start of the current authoritative day."""
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def install_clock(clock: Clock) -> None:
    """Replace the process-wide clock. The simulator does this at batch start."""
    _registry.install(clock)


def current_clock() -> Clock:
    return _registry.current


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Temporarily install a clock for this thread only."""
    with _registry.scoped(clock) as installed:
        yield installed


def to_ist(value: datetime) -> datetime:
    """Normalise any datetime to IST. Naive input is assumed to already be IST."""
    return _to_ist(value)


def in_contact_window(when: datetime, *, start_hour: int, end_hour: int) -> bool:
    """TRAI-01: is `when` inside the permitted commercial-contact window?

    The window is closed at both ends in the sense that a call must both start at
    or after `start_hour` and strictly before `end_hour`. Every day counts,
    including weekends and public holidays - the amendment removed the exemption.
    """
    local = _to_ist(when)
    return start_hour <= local.hour < end_hour


def next_contact_window_start(when: datetime, *, start_hour: int, end_hour: int) -> datetime:
    """The earliest instant at or after `when` that satisfies TRAI-01."""
    local = _to_ist(when)
    if in_contact_window(local, start_hour=start_hour, end_hour=end_hour):
        return local
    candidate = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.hour >= end_hour:
        candidate = candidate + timedelta(days=1)
    return candidate
