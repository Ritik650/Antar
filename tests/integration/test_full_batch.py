"""A full batch through all five layers, offline.

PLAN.md 9.1: *"**Integration** — full batch through all five layers against recorded
fixtures, offline."*

This is the only test that runs `run_pipeline`, and it is the one that would have caught
D20 (a channel with no template), D21 (the ledger silently replaced by a sink) and D22
(the oracle's choice recorded as the model's) without needing a person to read a trace
and disbelieve it. Every one of those bugs lived precisely in the seams between layers,
and no unit test looks at a seam.

It is deliberately small — a few hundred customers — because the property being tested
is *composition*, not scale. The M6 artifacts cover scale.
"""

from __future__ import annotations

import pytest

from antar.audit.ledger import Ledger
from antar.audit.replay import replay_is_self_consistent
from antar.audit.trace import STAGES, TraceIndex, build_trace
from antar.config import get_config
from antar.pipeline import run_pipeline
from antar.signals.schemas import LedgerKind

pytestmark = pytest.mark.integration

# Sized to clear `_fit_models`' 100-row exploration floor with margin: 500 customers
# yields 89 exploration rows and the whole module skips, which is a suite that passes
# by not running. 800 yields 165. Measured, not guessed.
CUSTOMERS = 800
MAX_EVENTS = 150


@pytest.fixture(scope="module")
def batch():
    """One pipeline run, shared by every test here.

    Module-scoped because the run takes tens of seconds and nothing below mutates it.
    """
    config = get_config().with_overrides(
        {
            "simulator.n_customers": CUSTOMERS,
            "simulator.checkout_abandon_events": CUSTOMERS // 3,
        }
    )
    # Deliberately no `except NoModel: skip`. An earlier version had one, the batch was
    # sized just below the fitting floor, and all fifteen tests skipped silently - the
    # exact "green because it did not run" failure this file exists to prevent
    # elsewhere. If the floor stops being met, this fixture should fail loudly.
    result, ledger = run_pipeline(
        "base", seed=11, config=config, ledger=Ledger(), max_events=MAX_EVENTS
    )
    return result, ledger


# ------------------------------------------------------- the whole path


def test_the_batch_actually_ran(batch):
    """First, because everything below is vacuous without it."""
    result, ledger = batch
    assert result.events > 0
    assert len(ledger) > 0
    assert result.model_version != "unfitted"


def test_every_event_reaches_a_decision(batch):
    result, _ = batch
    assert result.events == MAX_EVENTS
    assert result.decided == result.events, (
        "an event that is never decided on is an event nobody can account for"
    )


def test_every_event_is_contacted_abstained_or_refused(batch):
    result, _ = batch
    assert result.contacted + result.abstained + result.refused == result.events


def test_the_ledger_verifies_and_replays(batch):
    _, ledger = batch
    assert ledger.verify_chain().ok
    assert replay_is_self_consistent(ledger).ok


def test_every_layer_wrote_to_the_ledger(batch):
    """The seam test. A layer that runs but records nothing is invisible to the audit,
    and an audit with an invisible layer is not an audit."""
    _, ledger = batch
    kinds = {entry.kind for entry in ledger.entries()}
    expected = set(STAGES) - {LedgerKind.ACTION}
    assert expected <= kinds, f"no ledger entries from: {expected - kinds}"


def test_a_contact_produced_an_action_entry(batch):
    """D21 in test form. The gate wrote through a `LedgerSink`, and for a while that
    sink was a `NullLedger` substituted for the caller's empty one."""
    result, ledger = batch
    actions = ledger.entries(kind=LedgerKind.ACTION)
    if result.contacted == 0:
        pytest.skip("no contacts in this batch; nothing to assert about actions")
    assert len(actions) == result.contacted + result.refused, (
        f"{result.contacted} contacts and {result.refused} refusals produced "
        f"{len(actions)} ledger actions"
    )


# --------------------------------------------------------- the estimate


def test_the_recorded_uplift_came_from_a_model(batch):
    """D22. A decision that records an estimate must record one a model produced.

    The check that makes this meaningful is the *spread*: an oracle-driven or unfitted
    pipeline produces estimates that are constant, or that match ground truth exactly.
    A fitted model produces a distribution.
    """
    result, ledger = batch
    estimates = [
        entry.payload["uplift_estimate"]
        for entry in ledger.entries(kind=LedgerKind.DECISION)
    ]
    assert estimates
    assert result.model_version.startswith("x_learner-"), result.model_version
    assert len(set(estimates)) > 1, (
        "every decision recorded the same uplift, which is what an unfitted model "
        "looks like - and an unfitted model under a capacity constraint is 'contact "
        "everyone until the budget runs out'"
    )


def test_the_estimate_has_an_interval_around_it(batch):
    _, ledger = batch
    for entry in ledger.entries(kind=LedgerKind.DECISION):
        low, high = entry.payload["uplift_ci"]
        assert low <= entry.payload["uplift_estimate"] <= high


def test_every_decision_carries_the_versions_that_made_it(batch):
    _, ledger = batch
    for entry in ledger.entries(kind=LedgerKind.DECISION):
        assert entry.payload["model_version"] != "unversioned"
        assert entry.payload["policy_version"].startswith("pol-")


# ----------------------------------------------------------- the holdout


def test_the_holdout_is_decided_on_and_never_acted_on(batch):
    """N3, checked on the record rather than on the code.

    A control customer's decision is computed and written down - that is what makes the
    holdout a measurement rather than a hole in the log - and then nothing executes.
    """
    result, ledger = batch
    assert result.control > 0, "no holdout in this batch; the comparison is impossible"

    control_decisions = [
        entry.payload
        for entry in ledger.entries(kind=LedgerKind.DECISION)
        if entry.payload["is_control"]
    ]
    assert len(control_decisions) == result.control

    for decision in control_decisions:
        assert decision["chosen"] is None, "a holdout event was assigned an action"
        assert "RANDOMISED_HOLDOUT" in decision["binding_constraints"]

    control_ids = {d["decision_id"] for d in control_decisions}
    for action in ledger.entries(kind=LedgerKind.ACTION):
        assert action.payload["decision_id"] not in control_ids, (
            "an action exists for a holdout decision"
        )


def test_the_holdout_share_is_near_what_was_configured(batch):
    result, _ = batch
    share = result.control / result.events
    configured = float(get_config().get("eval.control_share"))
    # Wide tolerance on purpose: assignment is by customer, and a few hundred customers
    # produce a lumpy realised share. The property is "roughly the configured share",
    # not "exactly", and asserting exactness here would be asserting a coincidence.
    assert abs(share - configured) < 0.15, f"realised {share:.2f} vs configured {configured}"


# ------------------------------------------------------------ the trace


def test_a_trace_can_be_assembled_for_every_event(batch):
    _, ledger = batch
    index = TraceIndex(ledger)
    assert len(index.event_ids()) == MAX_EVENTS
    for event_id in index.event_ids():
        trace = build_trace(ledger, event_id)
        assert trace.found
        assert trace.narrate()


def test_a_contacted_trace_answers_why_that_and_why_then(batch):
    """The console's acceptance criterion, on a real run rather than a fixture."""
    result, ledger = batch
    if result.contacted == 0:
        pytest.skip("no contacts in this batch")

    index = TraceIndex(ledger)
    traces = [build_trace(ledger, eid) for eid in index.event_ids()]
    contacted = [t for t in traces if t.approved]
    assert contacted

    trace = contacted[0]
    narrative = trace.narrate()
    assert "L2 classified it as" in narrative        # why that
    assert "L3 estimated an uplift" in narrative     # why that
    assert "scheduled for" in narrative              # why then
    assert trace.versions["uplift_model"]
    assert trace.money["amount_paise"] > 0


def test_nothing_was_actually_sent(batch):
    """`gate.dry_run` defaults to true and no client is wired in. A test suite that
    could send an SMS is a test suite nobody should run twice."""
    _, ledger = batch
    for action in ledger.entries(kind=LedgerKind.ACTION):
        assert action.payload["executed"] is False
        assert action.payload["dry_run"] is True
        assert action.payload["provider_reference"] is None


def test_every_drafted_message_names_a_registered_template(batch):
    """D20 in test form: the act layer must be able to serve whatever L3 chose."""
    from antar.act.templates import load_templates

    registered = set(load_templates())
    _, ledger = batch
    for action in ledger.entries(kind=LedgerKind.ACTION):
        draft = action.payload.get("draft")
        if draft:
            assert draft["template_id"] in registered


def test_the_summary_and_the_ledger_agree(batch):
    """Two independent counts of the same run. They have disagreed before (D21)."""
    result, ledger = batch
    assert result.ledger_entries == len(ledger)
    assert result.ledger_head == ledger.head()
    assert len(ledger.entries(kind=LedgerKind.EVENT)) == result.events
    assert len(ledger.entries(kind=LedgerKind.DECISION)) == result.decided
