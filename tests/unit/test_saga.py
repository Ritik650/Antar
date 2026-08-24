"""Compensating transactions. PLAN.md section 10:

> Worker killed mid-saga → compensating transaction runs on restart; no orphaned charge
"""

from __future__ import annotations

import pytest

from antar.act.saga import IRREVERSIBLE_SEND, Saga, StepState


def test_a_clean_run_completes_every_step():
    log: list[str] = []
    saga = (
        Saga("recovery")
        .add("link", lambda: log.append("link") or "plink_1", lambda r: log.append(f"-{r}"))
        .add("send", lambda: log.append("send") or "msg_1", lambda r: log.append(f"-{r}"))
    )
    result = saga.run()
    assert result.ok and result.clean
    assert result.completed == ["link", "send"]
    assert log == ["link", "send"]


def test_a_failure_unwinds_the_completed_steps_in_reverse():
    """The order matters: undo the most recent thing first, or a compensation can
    depend on state a later step already removed."""
    order: list[str] = []
    saga = (
        Saga("recovery")
        .add("one", lambda: "a", lambda r: order.append("undo-one"))
        .add("two", lambda: "b", lambda r: order.append("undo-two"))
        .add("three", lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda r: None)
    )
    result = saga.run()

    assert not result.ok
    assert result.failed_step == "three"
    assert "boom" in result.error
    assert order == ["undo-two", "undo-one"]
    assert result.compensated == ["two", "one"]


def test_no_orphaned_charge_after_a_mid_saga_failure():
    """The named case. A payment link created before the failure is cancelled.

    Leaving it live means a customer can pay against a decision Antar withdrew - money
    arriving with no matching record.
    """
    live_links: set[str] = set()

    def create_link() -> str:
        live_links.add("plink_1")
        return "plink_1"

    def cancel_link(link_id: str) -> None:
        live_links.discard(link_id)

    saga = (
        Saga("recovery")
        .add("payment_link", create_link, cancel_link)
        .add("send", lambda: (_ for _ in ()).throw(ConnectionError("gateway down")),
             irreversible_reason=IRREVERSIBLE_SEND)
    )
    result = saga.run()

    assert not result.ok
    assert live_links == set(), "a live payment link was left behind"
    assert "payment_link" in result.compensated


def test_compensate_all_is_the_restart_path():
    """A worker that died between steps is recovered by replaying the log backwards."""
    undone: list[str] = []
    saga = (
        Saga("recovery")
        .add("link", lambda: "plink_1", lambda r: undone.append(r))
        .add("send", lambda: "msg_1", irreversible_reason=IRREVERSIBLE_SEND)
    )
    saga.run()

    # ...worker dies here, restarts, reloads the log, and unwinds.
    result = saga.compensate_all()
    assert undone == ["plink_1"]
    assert result.irreversible == ["send"]
    assert not result.clean, "an irreversible step means the unwind was not clean"


def test_compensating_twice_does_not_undo_twice():
    """Two crashes in a row must not produce two refunds."""
    undone: list[str] = []
    saga = Saga("recovery").add("link", lambda: "plink_1", lambda r: undone.append(r))
    saga.run()

    saga.compensate_all()
    saga.compensate_all()
    assert undone == ["plink_1"]


def test_a_delivered_message_is_declared_irreversible_rather_than_pretended_undone():
    """Antar does not auto-send a correction.

    An unprompted "please ignore our last message" is a second unsolicited contact, a
    second C-BUDGET slot, and a second RBI-EM-02 opt-out prompt. Claiming the send was
    compensated would be worse than admitting it was not.
    """
    saga = Saga("recovery").add(
        "send", lambda: "msg_1", irreversible_reason=IRREVERSIBLE_SEND
    )
    saga.run()
    result = saga.compensate_all()

    assert result.irreversible == ["send"]
    assert saga.steps[0].state is StepState.IRREVERSIBLE
    assert "opt-out prompt" in saga.steps[0].irreversible_reason


def test_a_step_without_compensation_must_state_why():
    """Silence is indistinguishable from an oversight, so it is refused."""
    with pytest.raises(ValueError, match="no compensation and no stated reason"):
        Saga("recovery").add("send", lambda: "msg_1")


def test_one_failed_compensation_does_not_strand_the_others():
    """A live link left behind because a refund failed is two problems, not one."""
    undone: list[str] = []
    saga = (
        Saga("recovery")
        .add("link", lambda: "plink_1", lambda r: undone.append(r))
        .add("charge", lambda: "pay_1",
             lambda r: (_ for _ in ()).throw(RuntimeError("refund API down")))
        .add("boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda r: None)
    )
    result = saga.run()

    assert undone == ["plink_1"], "the link should still have been cancelled"
    assert len(result.compensation_errors) == 1
    assert "refund API down" in result.compensation_errors[0]
    assert not result.clean


def test_the_trace_records_every_step_and_its_state():
    """An operator reading an aborted workflow needs the whole picture, not the error."""
    saga = (
        Saga("recovery")
        .add("link", lambda: "plink_1", lambda r: None)
        .add("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")), lambda r: None)
        .add("never", lambda: "n", lambda r: None)
    )
    saga.run()
    trace = saga.trace()

    assert [row["name"] for row in trace] == ["link", "boom", "never"]
    assert [row["state"] for row in trace] == ["COMPENSATED", "FAILED", "PENDING"]
    assert trace[1]["error"].startswith("RuntimeError")
    assert all(row["started_at"] for row in trace[:2])


def test_steps_that_never_ran_are_not_compensated():
    """Undoing something that never happened is its own kind of bug."""
    touched: list[str] = []
    saga = (
        Saga("recovery")
        .add("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")), lambda r: None)
        .add("later", lambda: "l", lambda r: touched.append("undo-later"))
    )
    saga.run()
    assert touched == []
    assert saga.steps[1].state is StepState.PENDING
