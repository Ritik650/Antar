"""The authoritative clock, and the rule that nothing else is allowed to be one."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from antar import clock

PACKAGE = Path(__file__).resolve().parents[2] / "antar"

# Anything that reads the wall clock behind the layer's back. Matched on the parse
# tree rather than with a regex, so that prose mentioning `datetime.now()` in a
# docstring is not an offence - only a call is.
FORBIDDEN_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
}

# clock.py is the one place allowed to know the real time. razorpay_client uses
# time.monotonic for the circuit breaker, which measures elapsed intervals rather
# than instants and is therefore immune to skew - the distinction that matters here.
EXEMPT = {"clock.py"}


def _wall_clock_calls(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in FORBIDDEN_CALLS:
                found.append((node.lineno, f"{pair[0]}.{pair[1]}()"))
    return found


def test_no_module_outside_clock_reads_the_wall_clock():
    """PLAN.md section 10, clock-skew row.

    A worker whose clock has drifted ten minutes ahead must not be able to schedule
    a 23-hour notification and call it 24. That guarantee only holds if there is
    exactly one clock, so the rule is checked mechanically rather than trusted.
    """
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path.name in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, call in _wall_clock_calls(tree):
            offenders.append(f"{path.relative_to(PACKAGE)}:{lineno}: {call}")
    assert not offenders, "use antar.clock.now() instead:\n" + "\n".join(offenders)


def test_the_discipline_check_actually_catches_an_offender():
    """A guard that cannot fail is not a guard."""
    tree = ast.parse("import datetime\nx = datetime.now()\n")
    assert _wall_clock_calls(tree) == [(2, "datetime.now()")]


def test_ist_is_a_fixed_offset():
    """ADR-0004. India has one timezone and no daylight saving."""
    assert clock.IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_frozen_clock_is_frozen(frozen_clock):
    assert clock.now() == clock.now()
    assert clock.now() == frozen_clock.now()


def test_use_clock_restores_the_previous_clock():
    outer = clock.now()
    with clock.use_clock(clock.FrozenClock(datetime(2030, 1, 1, tzinfo=clock.IST))):
        assert clock.now().year == 2030
    assert clock.now() == outer


def test_naive_datetimes_are_treated_as_ist():
    assert clock.to_ist(datetime(2026, 4, 28, 11, 4)).tzinfo == clock.IST


def test_simulated_clock_advances_only_when_ticked():
    sim = clock.SimulatedClock(datetime(2026, 4, 1, tzinfo=clock.IST), resolution_minutes=15)
    start = sim.now()
    assert sim.now() == start
    assert sim.tick(4) == start + timedelta(hours=1)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(8, False), (9, True), (12, True), (20, True), (21, False), (23, False), (0, False)],
)
def test_trai_contact_window_boundaries(hour, expected):
    """TRAI-01: no commercial contact before 09:00 or after 21:00, any day."""
    when = datetime(2026, 4, 28, hour, 30, tzinfo=clock.IST)
    assert clock.in_contact_window(when, start_hour=9, end_hour=21) is expected


def test_contact_window_applies_on_weekends_and_holidays():
    """The Second Amendment removed the weekend carve-out. 26 April 2026 is a Sunday."""
    sunday_evening = datetime(2026, 4, 26, 22, 0, tzinfo=clock.IST)
    assert not clock.in_contact_window(sunday_evening, start_hour=9, end_hour=21)


def test_next_window_start_rolls_forward_from_the_evening():
    late = datetime(2026, 4, 28, 22, 15, tzinfo=clock.IST)
    nxt = clock.next_contact_window_start(late, start_hour=9, end_hour=21)
    assert nxt == datetime(2026, 4, 29, 9, 0, tzinfo=clock.IST)


def test_next_window_start_stays_on_the_same_day_before_dawn():
    early = datetime(2026, 4, 28, 6, 0, tzinfo=clock.IST)
    nxt = clock.next_contact_window_start(early, start_hour=9, end_hour=21)
    assert nxt == datetime(2026, 4, 28, 9, 0, tzinfo=clock.IST)


def test_next_window_start_is_a_no_op_inside_the_window():
    inside = datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST)
    assert clock.next_contact_window_start(inside, start_hour=9, end_hour=21) == inside
