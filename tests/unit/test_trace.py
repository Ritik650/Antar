"""The one-click answer to *"why did you contact this customer at 11:04 on a Tuesday?"*

PLAN.md M8 makes that question the acceptance criterion for the console. A trace that
answers *what* without answering *why then* sounds like a rationalisation, so these
tests check both halves and check that a missing stage is reported rather than filled in.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from antar import clock
from antar.audit.ledger import Ledger
from antar.audit.trace import STAGES, Trace, TraceIndex, build_trace
from antar.signals.schemas import (
    ActionRecord,
    AtRiskEvent,
    Channel,
    Decision,
    Diagnosis,
    DraftedMessage,
    FailureClass,
    Intervention,
    InterventionClass,
    LedgerKind,
    LossClass,
    MerchantCategory,
    MessageClass,
    Outcome,
    PaymentMethod,
)

AT = datetime.fromisoformat("2026-08-25T11:04:00+05:30")
SCHEDULED = datetime.fromisoformat("2026-08-25T11:04:00+05:30")


def make_event(event_id: str = "evt_1") -> AtRiskEvent:
    return AtRiskEvent(
        event_id=event_id,
        merchant_id="mrch_1",
        customer_id="cust_1",
        loss_class=LossClass.MANDATE_FAILURE,
        subscription_id="sub_1",
        cycle_number=4,
        amount_paise=49900,
        merchant_category=MerchantCategory.OTT_SUBSCRIPTION,
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        occurred_at=AT,
        error_code="BAD_REQUEST_ERROR",
        error_reason="payment_failed",
    )


def make_diagnosis(event_id: str = "evt_1") -> Diagnosis:
    return Diagnosis(
        diagnosis_id="diag_1",
        event_id=event_id,
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        confidence=0.91,
        recommended_class=InterventionClass.CONTACT,
        source="table",
        model_version="detect-v1",
    )


def make_intervention(event_id: str = "evt_1") -> Intervention:
    return Intervention(
        intervention_id="int_1",
        event_id=event_id,
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        scheduled_for=SCHEDULED,
    )


def make_decision(event_id: str = "evt_1", **overrides) -> Decision:
    fields = {
        "decision_id": "dec_1",
        "event_id": event_id,
        "chosen": make_intervention(event_id),
        "uplift_estimate": 0.0412,
        "uplift_ci": (0.0112, 0.0712),
        "expected_incremental_paise": 2055,
        "binding_constraints": ["TRAI-01", "C-BUDGET"],
        "model_version": "x_learner-n554",
        "policy_version": "pol-abc123def456",
        "decided_at": AT,
        "rationale": "Selected by the allocator.",
    }
    fields.update(overrides)
    return Decision(**fields)


def make_action(*, executed: bool = True, rejected: str | None = None) -> ActionRecord:
    return ActionRecord(
        action_id="act_1",
        decision_id="dec_1",
        event_id="evt_1",
        idempotency_key="idem_1",
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        amount_paise=49900,
        cost_paise=25,
        executed=executed and rejected is None,
        dry_run=not executed,
        rejected_reason=rejected,
        provider_reference="sms_ref_1" if executed and rejected is None else None,
        attempted_at=AT,
        draft=DraftedMessage(
            template_id="RETRY_SCHEDULED_SMS",
            message_class=MessageClass.TRANSACTIONAL,
            slots={"reason": "not enough balance at the time"},
            rendered="Antar: your payment of Rs 499 could not be completed.",
            prompt_version="pv_abcdef012345",
        ),
    )


@pytest.fixture
def frozen():
    with clock.use_clock(clock.FrozenClock(AT)) as c:
        yield c


@pytest.fixture
def full_ledger(frozen) -> Ledger:
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DIAGNOSIS, make_diagnosis())
    led.append(LedgerKind.DECISION, make_decision())
    led.record_action(make_action())
    led.append(
        LedgerKind.OUTCOME,
        Outcome(
            event_id="evt_1",
            recovered=True,
            recovered_paise=49900,
            recovered_at=AT,
            attempts=1,
            contacts=1,
            cost_paise=25,
        ),
    )
    return led


# ------------------------------------------------------------ assembly


def test_a_complete_trace_has_every_stage(full_ledger):
    trace = build_trace(full_ledger, "evt_1")
    assert trace.found
    assert trace.gaps == []
    assert {e.kind for e in trace.entries} == set(STAGES)


def test_an_unknown_event_is_reported_as_not_found(full_ledger):
    trace = build_trace(full_ledger, "evt_does_not_exist")
    assert not trace.found
    assert "No ledger entries exist" in trace.narrate()


def test_the_trace_carries_the_verification_of_the_whole_ledger(full_ledger):
    """A break anywhere is a reason to distrust a trace from anywhere."""
    assert build_trace(full_ledger, "evt_1").verified.ok

    full_ledger._disable_append_only_guards()
    full_ledger._conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")
    full_ledger._conn.commit()

    trace = build_trace(full_ledger, "evt_1")
    assert not trace.verified.ok
    assert "WARNING" in trace.narrate()
    assert "unreliable" in trace.narrate()


# --------------------------------------------------- the answer fields


def test_the_trace_answers_why_that(full_ledger):
    trace = build_trace(full_ledger, "evt_1")
    estimate, (low, high) = trace.uplift
    assert estimate == pytest.approx(0.0412)
    assert (low, high) == pytest.approx((0.0112, 0.0712))
    assert trace.diagnosis["failure_class"] == "INSUFFICIENT_FUNDS"


def test_the_trace_answers_why_then(full_ledger):
    """The half a plausible-sounding trace leaves out."""
    trace = build_trace(full_ledger, "evt_1")
    assert trace.contacted_at == AT
    assert trace.binding_constraints == ["TRAI-01", "C-BUDGET"]
    narrative = trace.narrate()
    assert "TRAI-01" in narrative and "C-BUDGET" in narrative


def test_every_version_that_touched_the_event_is_recoverable(full_ledger):
    versions = build_trace(full_ledger, "evt_1").versions
    assert versions == {
        "detector": "detect-v1",
        "uplift_model": "x_learner-n554",
        "policy": "pol-abc123def456",
        "prompt": "pv_abcdef012345",
    }


def test_the_money_adds_up(full_ledger):
    money = build_trace(full_ledger, "evt_1").money
    assert money["amount_paise"] == 49900
    assert money["recovered_paise"] == 49900
    assert money["cost_paise"] == 25


def test_an_executed_action_shows_the_text_that_went_out(full_ledger):
    narrative = build_trace(full_ledger, "evt_1").narrate()
    assert "SMS sent at" in narrative
    assert "The message was:" in narrative
    assert "sms_ref_1" in narrative


# ------------------------------------------------------ partial traces


def test_a_missing_stage_is_named_not_invented(frozen):
    """Most events end at "no action was worth taking". That is a normal state."""
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DIAGNOSIS, make_diagnosis())

    trace = build_trace(led, "evt_1")
    assert trace.gaps == ["DECISION", "ACTION", "OUTCOME"]
    narrative = trace.narrate()
    assert "No ledger entry exists for: DECISION, ACTION, OUTCOME" in narrative
    assert "reported rather than inferred" in narrative


def test_an_abstention_says_no_action_was_taken(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DIAGNOSIS, make_diagnosis())
    led.append(
        LedgerKind.DECISION,
        make_decision(chosen=None, binding_constraints=["VALUE_BELOW_ZERO"]),
    )

    narrative = build_trace(led, "evt_1").narrate()
    assert "chose to take no action" in narrative
    assert "VALUE_BELOW_ZERO" in narrative


def test_a_holdout_event_says_so_and_cites_n3(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(
        LedgerKind.DECISION,
        make_decision(chosen=None, is_control=True, binding_constraints=["RANDOMISED_HOLDOUT"]),
    )

    narrative = build_trace(led, "evt_1").narrate()
    assert "randomised holdout" in narrative
    assert "N3" in narrative
    # The holdout is not a *timing* constraint, and describing it as one would make
    # the sentence say the opposite of what happened.
    assert "binding constraints were" not in narrative


def test_a_refusal_says_what_was_refused_and_that_nothing_was_sent(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DECISION, make_decision())
    led.record_action(
        make_action(executed=False, rejected="TRAI-03: promotional content detected")
    )

    trace = build_trace(led, "evt_1")
    assert trace.refusals
    assert not trace.approved
    narrative = trace.narrate()
    assert "The gate refused" in narrative
    assert "Nothing was sent" in narrative


def test_a_dry_run_reads_as_a_deliberate_non_send_not_a_refusal(frozen):
    """Under `dry_run` the gate approves and does not execute. A trace that called
    that a refusal would misreport every evaluation run."""
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DECISION, make_decision())
    led.record_action(make_action(executed=False))

    trace = build_trace(led, "evt_1")
    assert trace.approved and not trace.executed and trace.dry_run
    narrative = trace.narrate()
    assert "deliberately not sent" in narrative
    assert "would have been" in narrative
    assert "refused" not in narrative


def test_an_optout_is_counted_against_the_policy(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DECISION, make_decision())
    led.record_action(make_action())
    led.append(
        LedgerKind.OUTCOME,
        Outcome(event_id="evt_1", recovered=False, optout=True, optout_at=AT, contacts=1),
    )

    narrative = build_trace(led, "evt_1").narrate()
    assert "opted out" in narrative
    assert "counted against the policy" in narrative


# --------------------------------------------------------------- index


def test_the_index_lists_every_event_once(frozen):
    led = Ledger()
    for eid in ("evt_1", "evt_2", "evt_1"):
        led.append(LedgerKind.EVENT, make_event(eid))
    assert TraceIndex(led).event_ids() == ["evt_1", "evt_2"]


def test_one_events_entries_do_not_leak_into_anothers_trace(frozen):
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event("evt_1"))
    led.append(LedgerKind.EVENT, make_event("evt_2"))
    led.append(LedgerKind.DECISION, make_decision("evt_2"))

    assert build_trace(led, "evt_1").decision is None
    assert build_trace(led, "evt_2").decision is not None


def test_as_dict_is_json_shaped_for_the_console(full_ledger):
    import json

    payload = build_trace(full_ledger, "evt_1").as_dict()
    assert json.loads(json.dumps(payload))["event_id"] == "evt_1"
    assert payload["narrative"]


def test_a_trace_is_a_frozen_view(full_ledger):
    trace = build_trace(full_ledger, "evt_1")
    assert isinstance(trace, Trace)
    # Frozen because a trace is evidence: a caller that could edit one could show the
    # console a story the ledger does not contain.
    with pytest.raises(AttributeError):
        trace.event_id = "something_else"  # type: ignore[misc]


# ------------------------------------------- the index and the single trace


def test_the_index_and_the_single_trace_agree(full_ledger):
    """`TraceIndex` exists purely for speed. Speed is worthless if it changes answers.

    `build_trace` rescans and re-verifies the whole ledger per call; the index does one
    pass and groups. They must produce identical traces, and this asserts it field by
    field rather than trusting the two code paths to stay aligned. POSTMORTEM D26.
    """
    index = TraceIndex(full_ledger)
    for event_id in index.event_ids():
        slow = build_trace(full_ledger, event_id)
        fast = index.trace(event_id)
        assert fast.as_dict() == slow.as_dict(), f"the two paths disagree on {event_id}"


def test_the_index_links_an_action_to_its_event_through_the_decision(frozen):
    """The hard case the single-pass grouping has to get right.

    An `ActionRecord` carries `event_id` directly, but an entry that carried only a
    `decision_id` would be orphaned by naive grouping. The index resolves it through the
    decision that claimed that id.
    """
    led = Ledger()
    led.append(LedgerKind.EVENT, make_event())
    led.append(LedgerKind.DECISION, make_decision())
    # An entry with no event_id at all, reachable only through the decision.
    led.append(LedgerKind.ALERT, {"decision_id": "dec_1", "alert": "AWAITING_APPROVAL"})

    trace = TraceIndex(led).trace("evt_1")
    assert len(trace.entries) == 3
    assert trace.alerts and trace.alerts[0]["alert"] == "AWAITING_APPROVAL"


def test_the_index_verifies_the_chain_once_and_shares_the_result(full_ledger):
    index = TraceIndex(full_ledger)
    assert index.verified.ok
    assert all(trace.verified is index.verified for trace in index.traces())


def test_the_index_reports_a_broken_chain_on_every_trace(full_ledger):
    full_ledger._disable_append_only_guards()
    full_ledger._conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")
    full_ledger._conn.commit()

    index = TraceIndex(full_ledger)
    assert not index.verified.ok
    assert all("WARNING" in trace.narrate() for trace in index.traces())
