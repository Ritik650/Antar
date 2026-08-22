"""Property tests for the constraint compiler and the gate.

PLAN.md M4 acceptance:

> Property test (hypothesis): for any random candidate set, no solution returned by
> the compiler violates any `BLOCKING` regulation. Run >= 1,000 examples.

The value here is not that the compiler passes on the cases I thought of. It is that
hypothesis generates the cases I did not — the 24-hour lead time landing exactly on
the boundary, the customer whose consent expired the same second the batch ran, the
candidate set where every action targets one customer.

The second property is the important one: **the compiler and the naive validator must
agree.** They share no code, so a disagreement is a real bug in one of them, and
hypothesis is much better than I am at finding the input where they diverge.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from antar import clock
from antar.ids import intervention_id
from antar.policy import regulations as reg
from antar.policy.compiler import Candidate, RowKind, compile_problem
from antar.policy.regulations import Severity
from antar.policy.validator import validate
from antar.signals.schemas import (
    AtRiskEvent,
    Channel,
    ConsentBasis,
    CustomerContext,
    DecisionContext,
    Intervention,
    LossClass,
    MandateState,
    MerchantCategory,
    MessageClass,
    PaymentMethod,
)

NOW = datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST)
GENERAL_CEILING = 1_500_000
HIGH_CEILING = 10_000_000
HIGH_CEILING_CATEGORIES = {
    MerchantCategory.INSURANCE,
    MerchantCategory.MUTUAL_FUND,
    MerchantCategory.CREDIT_CARD_BILL,
}

# Deliberately generous ranges. The interesting cases are on the boundaries, so the
# strategies straddle every threshold the regulations name.
contact_channels = st.sampled_from([Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL])
any_channel = st.sampled_from(list(Channel))
message_classes = st.sampled_from([MessageClass.TRANSACTIONAL, MessageClass.SERVICE])
mandate_states = st.sampled_from(list(MandateState))
consent_bases = st.sampled_from(list(ConsentBasis))
categories = st.sampled_from(list(MerchantCategory))


@st.composite
def decision_contexts(draw, customer_pool: int = 4) -> DecisionContext:
    customer_id = f"cust_{draw(st.integers(0, customer_pool - 1)):03d}"
    category = draw(categories)
    ceiling = HIGH_CEILING if category in HIGH_CEILING_CATEGORIES else GENERAL_CEILING

    event = AtRiskEvent(
        event_id=f"mer1_evt_{draw(st.integers(0, 6)):03d}",
        merchant_id="mer1",
        customer_id=customer_id,
        loss_class=LossClass.MANDATE_FAILURE,
        subscription_id=f"sub_{customer_id}",
        cycle_number=draw(st.integers(1, 6)),
        amount_paise=draw(st.integers(1_000, 12_000_000)),
        merchant_category=category,
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        occurred_at=NOW,
        error_reason="insufficient_funds",
    )

    granted_days_ago = draw(st.integers(0, 40))
    customer = CustomerContext(
        customer_id=customer_id,
        merchant_id="mer1",
        consent_basis=draw(consent_bases),
        consent_granted_at=NOW - timedelta(days=granted_days_ago),
        dnd_registered=draw(st.booleans()),
        optout_received=draw(st.booleans()),
        promise_to_pay_at=draw(st.one_of(st.none(), st.just(NOW + timedelta(days=2)))),
        mandate_state=draw(mandate_states),
        contacts_in_window=draw(st.integers(0, 5)),
        last_contact_at=draw(
            st.one_of(st.none(), st.integers(0, 200).map(lambda h: NOW - timedelta(hours=h)))
        ),
    )

    # Straddle the 24-hour boundary, and the 10:00/21:00 window edges.
    hours_ahead = draw(st.floats(min_value=0.0, max_value=96.0, allow_nan=False))
    scheduled = NOW + timedelta(hours=hours_ahead)
    scheduled = scheduled.replace(hour=draw(st.integers(0, 23)), minute=draw(st.integers(0, 59)))

    intervention = Intervention(
        intervention_id=intervention_id(event.event_id, "x", scheduled, 0),
        event_id=event.event_id,
        channel=draw(any_channel),
        message_class=draw(message_classes),
        scheduled_for=scheduled,
        discount_paise=draw(st.integers(0, min(event.amount_paise, 500_000))),
        requires_afa=draw(st.booleans()),
    )

    return DecisionContext(
        event=event,
        customer=customer,
        intervention=intervention,
        now=NOW,
        afa_free_ceiling_paise=ceiling,
        contact_window_start_hour=10,
        contact_window_end_hour=21,
        contacts_allowed_per_30d=3,
        cooldown_hours=72,
        explicit_consent_validity_days=7,
    )


@st.composite
def candidate_sets(draw, max_size: int = 8) -> list[Candidate]:
    contexts = draw(st.lists(decision_contexts(), min_size=1, max_size=max_size))
    return [Candidate(candidate_id=f"cand_{i}", context=ctx) for i, ctx in enumerate(contexts)]


SETTINGS = settings(
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------


@given(candidates=candidate_sets())
@SETTINGS
def test_no_feasible_candidate_violates_a_blocking_regulation(candidates):
    """The M4 acceptance property, stated directly."""
    problem = compile_problem(candidates)
    for candidate in problem.feasible:
        for rule in reg.REGULATIONS:
            if rule.severity is Severity.BLOCKING:
                assert not rule.violated_by(candidate.context), (
                    f"{candidate.candidate_id} survived compilation while violating "
                    f"{rule.id}"
                )


@given(candidates=candidate_sets())
@SETTINGS
def test_every_rejection_names_a_real_violated_regulation(candidates):
    """A rejection must be justifiable, not merely asserted."""
    problem = compile_problem(candidates)
    by_id = {c.candidate_id: c for c in candidates}
    for rejection in problem.rejected:
        assert rejection.regulation_ids
        context = by_id[rejection.candidate_id].context
        for rule_id in rejection.regulation_ids:
            assert reg.get(rule_id).violated_by(context), (
                f"{rejection.candidate_id} was rejected citing {rule_id}, which holds"
            )


@given(candidates=candidate_sets())
@SETTINGS
def test_compiler_partitions_the_input_exactly(candidates):
    """Nothing is lost and nothing is duplicated."""
    problem = compile_problem(candidates)
    assert len(problem.feasible) + len(problem.rejected) == len(candidates)
    ids = {c.candidate_id for c in problem.feasible} | {r.candidate_id for r in problem.rejected}
    assert ids == {c.candidate_id for c in candidates}


@given(candidates=candidate_sets())
@SETTINGS
def test_the_compiler_and_the_naive_validator_agree(candidates):
    """**The bug-catcher.** Two implementations, no shared code.

    Anything the compiler declares feasible, the validator must accept when selected
    alone. A disagreement is a real defect in one of them - most likely an off-by-one
    on a threshold, which is exactly the kind of thing a single implementation and a
    test written by the same person will not find.
    """
    problem = compile_problem(candidates)
    for candidate in problem.feasible:
        result = validate(
            [candidate],
            contacts_per_30d=3,
            cooldown_hours=72,
            lead_time_hours=24,
            window_start_hour=10,
            window_end_hour=21,
            explicit_consent_validity_days=7,
        )
        assert result.ok, (
            f"compiler accepted {candidate.candidate_id} but the independent validator "
            f"rejected it:\n{result.report()}"
        )


@given(candidates=candidate_sets())
@SETTINGS
def test_selecting_everything_feasible_respects_every_row(candidates):
    """Rows are the only thing standing between a greedy solver and a budget breach,
    so a row that a full selection satisfies vacuously is a row that does nothing."""
    problem = compile_problem(candidates, contacts_per_30d=3)
    selection = dict.fromkeys(problem.feasible_ids, 1.0)
    for row in problem.rows:
        if row.kind in (RowKind.CONTACT_BUDGET, RowKind.CUSTOMER_COOLDOWN):
            # These may legitimately be violated by selecting everything - that is
            # what the solver is for. Assert instead that they are non-vacuous.
            assert row.coefficients
            assert row.bound >= 0
        else:
            assert row.slack(selection) <= row.bound


@given(candidates=candidate_sets())
@SETTINGS
def test_rows_only_reference_feasible_candidates(candidates):
    """A row mentioning a rejected candidate would give the solver a variable it must
    not be able to set."""
    problem = compile_problem(candidates)
    feasible = problem.feasible_ids
    for row in problem.rows:
        assert set(row.coefficients) <= feasible, row.row_id


@given(candidates=candidate_sets())
@SETTINGS
def test_compilation_is_deterministic(candidates):
    first = compile_problem(candidates)
    second = compile_problem(candidates)
    assert first.feasible_ids == second.feasible_ids
    assert [r.row_id for r in first.rows] == [r.row_id for r in second.rows]
    assert [r.bound for r in first.rows] == [r.bound for r in second.rows]


@given(context=decision_contexts())
@SETTINGS
def test_predicates_never_raise(context):
    """A regulation that throws on an odd context would take down a whole batch."""
    for rule in reg.REGULATIONS:
        assert isinstance(rule.holds(context), bool)
        assert isinstance(rule.violated_by(context), bool)


@given(context=decision_contexts())
@SETTINGS
def test_an_opted_out_customer_is_never_contactable(context):
    """RBI-EM-02 has no exceptions and no grace window."""
    assume(context.customer.optout_received)
    assume(context.intervention.channel in {Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL})
    problem = compile_problem([Candidate("c", context)])
    assert not problem.feasible


@given(context=decision_contexts(), over_by=st.integers(1, 5_000_000))
@SETTINGS
def test_an_above_ceiling_debit_is_never_selected_without_afa(context, over_by):
    """RBI-EM-03/-04, on both sides of a category-dependent threshold.

    Constructed rather than filtered: most generated amounts sit below the ceiling,
    so `assume()` discarded almost everything and hypothesis rightly complained that
    the surviving distribution no longer tested what it claimed to.
    """
    over = context.model_copy(
        update={
            "event": context.event.model_copy(
                update={"amount_paise": context.afa_free_ceiling_paise + over_by}
            ),
            "intervention": context.intervention.model_copy(update={"requires_afa": False}),
        }
    )
    assert not compile_problem([Candidate("c", over)]).feasible

    routed = over.model_copy(
        update={"intervention": over.intervention.model_copy(update={"requires_afa": True})}
    )
    # Routing through AFA clears *this* rule; other rules may still reject it, so the
    # assertion is that RBI-EM-03 specifically is satisfied.
    assert reg.get("RBI-EM-03").holds(routed)


@given(context=decision_contexts())
@SETTINGS
def test_the_lead_time_boundary_is_inclusive(context):
    """'At least 24 hours' means 24.0 passes and 23.999 does not."""
    exactly = context.model_copy(
        update={
            "intervention": context.intervention.model_copy(
                update={"scheduled_for": context.now + timedelta(hours=24)}
            )
        }
    )
    just_under = context.model_copy(
        update={
            "intervention": context.intervention.model_copy(
                update={"scheduled_for": context.now + timedelta(hours=24) - timedelta(seconds=1)}
            )
        }
    )
    assert reg.get("RBI-EM-01").holds(exactly)
    assert not reg.get("RBI-EM-01").holds(just_under)
