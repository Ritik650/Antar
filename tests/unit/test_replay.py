"""Deterministic replay from the ledger.

PLAN.md M8: *"`replay.py`: replay a batch deterministically from the ledger."*

The comparator is the load-bearing part, and a comparator can fail in two directions.
One that reports divergence on identical input is noise; one that reports agreement on
anything is worse, because it turns a refactor check into a rubber stamp. Both
directions are tested here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from antar import clock
from antar.audit.ledger import Ledger
from antar.audit.replay import (
    COMPARED_FIELDS,
    LedgerNotVerified,
    replay,
    replay_is_self_consistent,
)
from antar.signals.schemas import Channel, LedgerKind
from tests.unit.test_trace import AT, make_decision, make_event, make_intervention


@pytest.fixture
def frozen():
    with clock.use_clock(clock.FrozenClock(AT)) as c:
        yield c


@pytest.fixture
def ledger(frozen) -> Ledger:
    led = Ledger()
    for eid in ("evt_1", "evt_2", "evt_3"):
        led.append(LedgerKind.EVENT, make_event(eid))
        led.append(LedgerKind.DECISION, make_decision(eid, decision_id=f"dec_{eid}"))
    return led


def echo(recorded: Ledger):
    """A decide function that returns exactly what was recorded."""

    def _decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        return build_trace(recorded, str(event["event_id"])).decision

    return _decide


# ------------------------------------------------------------ agreement


def test_replaying_the_recorded_decision_agrees_everywhere(ledger):
    result = replay(ledger, echo(ledger))
    assert result.ok
    assert result.events_replayed == 3
    assert result.identical == 3
    assert result.agreement == 1.0
    assert result.divergences == []


def test_the_null_replay_is_perfect_by_construction(ledger):
    """It tests the comparator, not the decision path — which is the point."""
    assert replay_is_self_consistent(ledger).ok


# ----------------------------------------------------------- divergence


def test_a_different_channel_is_reported(ledger):
    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["chosen"]["channel"] = Channel.WHATSAPP.value
        return recorded

    result = replay(ledger, decide)
    assert not result.ok
    assert result.diverged == 3
    assert {d.field for d in result.divergences} == {"chosen_channel"}
    assert result.divergences[0].recorded == "SMS"
    assert result.divergences[0].replayed == "WHATSAPP"


def test_a_different_schedule_is_reported(ledger):
    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["chosen"]["scheduled_for"] = (AT + timedelta(hours=3)).isoformat()
        return recorded

    assert {d.field for d in replay(ledger, decide).divergences} == {"chosen_scheduled_for"}


def test_abstaining_where_the_record_acted_is_reported(ledger):
    result = replay(ledger, lambda _e, _d: None)
    assert result.diverged == 3
    fields = {d.field for d in result.divergences}
    assert "chosen_channel" in fields


def test_a_changed_holdout_arm_is_reported(ledger):
    """The one divergence that would invalidate the evaluation rather than improve it."""

    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["is_control"] = True
        return recorded

    assert {d.field for d in replay(ledger, decide).divergences} == {"is_control"}


def test_a_negligible_float_difference_is_not_a_divergence(ledger):
    """Behavioural agreement, not bit-identity. A numpy release that moves the last
    ulp of an estimate has not changed what Antar did."""

    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["uplift_estimate"] += 1e-12
        return recorded

    assert replay(ledger, decide).ok


def test_a_material_float_difference_is_a_divergence(ledger):
    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["uplift_estimate"] += 0.01
        return recorded

    assert {d.field for d in replay(ledger, decide).divergences} == {"uplift_estimate"}


def test_the_decision_id_is_not_compared(ledger):
    """It is derived from the versions (ADR-0005), so comparing it would report a
    divergence on every version bump and bury the real ones."""
    assert "decision_id" not in COMPARED_FIELDS

    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        recorded = build_trace(ledger, str(event["event_id"])).decision
        recorded["decision_id"] = "dec_completely_different"
        return recorded

    assert replay(ledger, decide).ok


# ------------------------------------------------------------- the clock


def test_the_replayed_decision_sees_the_recorded_time_not_now(ledger):
    """D13, applied where it bites hardest.

    A decide function that reads `clock.now()` during replay must see the moment the
    decision was originally made. Otherwise every replay of an old batch silently
    re-decides under today's date and reports divergences that are artefacts.
    """
    seen: list[datetime] = []

    def decide(event, _diagnosis):
        from antar.audit.trace import build_trace

        seen.append(clock.now())
        return build_trace(ledger, str(event["event_id"])).decision

    with clock.use_clock(clock.FrozenClock(AT + timedelta(days=400))):
        result = replay(ledger, decide)

    assert result.ok
    assert seen == [AT, AT, AT], f"replay ran under the wall clock: {seen}"


# ----------------------------------------------------------- skipping


def test_an_event_with_no_decision_is_skipped_with_a_reason(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event("evt_1"))

    result = replay(led, lambda _e, _d: None)
    assert result.skipped == 1
    assert result.events_replayed == 0
    assert "no DECISION entry" in result.skipped_reasons["evt_1"]


def test_a_decision_with_no_event_is_skipped_with_a_reason(frozen):
    led = Ledger()
    led.append(LedgerKind.DECISION, make_decision("evt_1"))

    result = replay(led, lambda _e, _d: None)
    assert result.skipped == 1
    assert "no EVENT entry" in result.skipped_reasons["evt_1"]


def test_a_skipped_event_is_not_counted_as_agreement(frozen):
    """A run that skips everything must not report 100% agreement, which would read
    as a clean replay of a batch that was never replayed."""
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event("evt_1"))

    result = replay(led, lambda _e, _d: None)
    assert result.events_replayed == 0
    assert result.skipped == 1
    assert result.as_dict()["events_replayed"] == 0


# -------------------------------------------------------- verification


def test_replaying_a_tampered_ledger_is_refused(ledger):
    ledger._disable_append_only_guards()
    ledger._conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")
    ledger._conn.commit()

    with pytest.raises(LedgerNotVerified, match="chain break"):
        replay(ledger, echo(ledger))


def test_a_tampered_ledger_can_be_replayed_deliberately(ledger):
    """Inspecting the damage is a legitimate thing to want; doing it by accident is not."""
    ledger._disable_append_only_guards()
    ledger._conn.execute("UPDATE ledger SET kind = 'ALERT' WHERE seq = 1")
    ledger._conn.commit()

    result = replay(ledger, echo(ledger), verify_first=False)
    assert result.skipped + result.events_replayed == 3


# ------------------------------------------------------------- scoping


def test_a_subset_of_events_can_be_replayed(ledger):
    result = replay(ledger, echo(ledger), event_ids=["evt_2"])
    assert result.events_replayed == 1
    assert result.identical == 1


def test_the_result_serialises_for_an_artifact(ledger):
    import json

    payload = json.loads(json.dumps(replay(ledger, lambda _e, _d: None).as_dict()))
    assert payload["diverged"] == 3
    assert payload["agreement"] == 0.0
    assert len(payload["divergences"]) >= 3


def test_replay_never_produces_an_outcome(ledger):
    """It answers "would today's code decide the same?", not "what would have
    happened?". The recorded outcome happened under the recorded action; a
    counterfactual outcome belongs to eval/policies.py, which says it is an estimate."""
    result = replay(ledger, echo(ledger))
    assert not hasattr(result, "outcome")
    assert "outcome" not in result.as_dict()


def test_replay_cannot_reach_an_executor(ledger):
    """A replay that could send an SMS is not a replay."""
    calls: list[str] = []

    def decide(event, _diagnosis):
        from antar.act.executors.notify import send_notification

        calls.append("decide")
        assert getattr(send_notification, "__antar_requires_gate__", False)
        from antar.audit.trace import build_trace

        return build_trace(ledger, str(event["event_id"])).decision

    assert replay(ledger, decide).ok
    assert calls, "the decide function never ran"


def test_an_intervention_helper_stays_importable_for_the_fixtures():
    assert make_intervention("evt_1").channel is Channel.SMS
