"""The policy layer: regulations, compiler, validator, gate, budgets."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from antar import clock
from antar.ids import idempotency_key, intervention_id
from antar.policy import regulations as reg
from antar.policy.budgets import BudgetPolicy, ContactLedger
from antar.policy.compiler import Candidate, RowKind, compile_problem, explain_rejection
from antar.policy.gate import (
    GateLimits,
    GateRefusal,
    NullLedger,
    PolicyGate,
    requires_gate,
)
from antar.policy.regulations import Severity, Verification
from antar.policy.validator import validate
from antar.signals.schemas import (
    ActionRecord,
    AtRiskEvent,
    Channel,
    ConsentBasis,
    CustomerContext,
    Decision,
    DecisionContext,
    DraftedMessage,
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


def make_event(*, amount_paise: int = 49900, category=MerchantCategory.OTT_SUBSCRIPTION):
    return AtRiskEvent(
        event_id="mer1_evt_0001",
        merchant_id="mer1",
        customer_id="cust_1",
        loss_class=LossClass.MANDATE_FAILURE,
        subscription_id="sub_1",
        cycle_number=2,
        amount_paise=amount_paise,
        merchant_category=category,
        method=PaymentMethod.UPI_AUTOPAY,
        issuer="HDFC",
        occurred_at=NOW,
        error_reason="insufficient_funds",
    )


def make_context(
    *,
    hours_ahead: float = 25,
    hour: int | None = None,
    channel: Channel = Channel.SMS,
    message_class: MessageClass = MessageClass.TRANSACTIONAL,
    amount_paise: int = 49900,
    category=MerchantCategory.OTT_SUBSCRIPTION,
    requires_afa: bool = False,
    discount_paise: int = 0,
    customer: CustomerContext | None = None,
    ceiling: int = GENERAL_CEILING,
) -> DecisionContext:
    scheduled = NOW + timedelta(hours=hours_ahead)
    if hour is not None:
        scheduled = scheduled.replace(hour=hour, minute=0)
    event = make_event(amount_paise=amount_paise, category=category)
    return DecisionContext(
        event=event,
        customer=customer or CustomerContext(customer_id="cust_1", merchant_id="mer1"),
        intervention=Intervention(
            intervention_id=intervention_id(event.event_id, channel, scheduled, discount_paise),
            event_id=event.event_id,
            channel=channel,
            message_class=message_class,
            scheduled_for=scheduled,
            discount_paise=discount_paise,
            requires_afa=requires_afa,
        ),
        now=NOW,
        afa_free_ceiling_paise=ceiling,
        contact_window_start_hour=10,
        contact_window_end_hour=21,
    )


def candidate(cid: str = "cand_1", **kwargs) -> Candidate:
    return Candidate(candidate_id=cid, context=make_context(**kwargs))


# ------------------------------------------------------------- the register


def test_blocking_rules_are_verified():
    """PLAN.md section 3: an unverified rule may not block money."""
    for rule in reg.REGULATIONS:
        if rule.severity is Severity.BLOCKING:
            assert rule.verification in reg.VERIFIABLE_ENOUGH_TO_BLOCK, (
                f"{rule.id} is BLOCKING but only {rule.verification.value}-verified"
            )


def test_every_regulation_has_a_citation():
    for rule in reg.REGULATIONS:
        assert rule.citation_url, f"{rule.id} has no citation"
        assert rule.human_explanation, f"{rule.id} has no explanation"
        assert rule.clause, f"{rule.id} has no clause reference"


def test_secondary_sourced_rules_are_advisory_and_say_why():
    for rule in reg.REGULATIONS:
        if rule.verification in (Verification.SECONDARY, Verification.UNVERIFIED):
            assert rule.severity is Severity.ADVISORY
            assert rule.note, f"{rule.id} is unverified but does not say so in its note"


def test_a_blocking_rule_on_a_secondary_source_cannot_be_constructed():
    """The constraint is enforced at construction, not merely asserted in a test."""
    with pytest.raises(ValueError, match="may not block money"):
        reg.Regulation(
            id="BAD-01",
            title="x",
            source="a blog",
            citation_url="https://example.com",
            clause="para 1",
            effective_date=date(2026, 1, 1),
            severity=Severity.BLOCKING,
            verification=Verification.SECONDARY,
            human_explanation="x",
            predicate=lambda _c: True,
        )


def test_the_verified_contact_window_opens_at_ten():
    """The correction verification found. PLAN.md said 09:00; the gazette says the
    08:00-10:00 band is default OFF, so the window opens at 10:00."""
    rule = reg.get("TRAI-01")
    assert "10:00" in rule.title
    assert "PLAN.md" in rule.note
    assert not rule.holds(make_context(hour=9, hours_ahead=25))
    assert rule.holds(make_context(hour=10, hours_ahead=25))


def test_fastag_exemption_is_considered_and_excluded():
    """RBI-EM-07. Asserted so the exemption is visibly out of scope, not overlooked."""
    rule = reg.get("RBI-EM-07")
    assert rule.severity is Severity.ADVISORY
    assert "FASTag" in rule.quote and "NCMC" in rule.quote
    assert "out of scope" in rule.human_explanation.lower()


# ---------------------------------------------------------------- predicates


def test_lead_time_is_at_least_not_more_than():
    """RBI-EM-01 says 'at least 24 hours'. Exactly 24 must pass."""
    rule = reg.get("RBI-EM-01")
    assert rule.holds(make_context(hours_ahead=24))
    assert rule.holds(make_context(hours_ahead=24.0001))
    assert not rule.holds(make_context(hours_ahead=23.9))


def test_optout_is_absolute():
    opted_out = CustomerContext(
        customer_id="cust_1", merchant_id="mer1", optout_received=True
    )
    assert not reg.get("RBI-EM-02").holds(make_context(customer=opted_out))


def test_afa_ceiling_is_category_dependent():
    """RBI-EM-04: the same amount is AFA-free for an insurer and not for an OTT."""
    rule = reg.get("RBI-EM-03")
    over_general = make_context(amount_paise=2_500_000, ceiling=GENERAL_CEILING)
    assert not rule.holds(over_general)
    assert rule.holds(
        make_context(amount_paise=2_500_000, ceiling=GENERAL_CEILING, requires_afa=True)
    )
    assert rule.holds(
        make_context(
            amount_paise=2_500_000, category=MerchantCategory.INSURANCE, ceiling=HIGH_CEILING
        )
    )


def test_paused_and_revoked_mandates_are_not_retry_candidates():
    rule = reg.get("RBI-EM-06")
    for state in (MandateState.PAUSED, MandateState.REVOKED, MandateState.COMPLETED):
        customer = CustomerContext(customer_id="c", merchant_id="m", mandate_state=state)
        assert not rule.holds(make_context(customer=customer)), state
    for state in (MandateState.ACTIVE, MandateState.HALTED):
        customer = CustomerContext(customer_id="c", merchant_id="m", mandate_state=state)
        assert rule.holds(make_context(customer=customer)), state


def test_inferred_consent_dies_with_the_contract():
    rule = reg.get("TRAI-04")
    live = CustomerContext(customer_id="c", merchant_id="m", consent_basis=ConsentBasis.INFERRED)
    dead = live.model_copy(update={"mandate_state": MandateState.REVOKED})
    assert rule.holds(make_context(customer=live))
    assert not rule.holds(make_context(customer=dead))


def test_explicit_consent_expires():
    rule = reg.get("TRAI-04")
    fresh = CustomerContext(
        customer_id="c", merchant_id="m",
        consent_basis=ConsentBasis.EXPLICIT, consent_granted_at=NOW - timedelta(days=3),
    )
    stale = fresh.model_copy(update={"consent_granted_at": NOW - timedelta(days=30)})
    assert rule.holds(make_context(customer=fresh))
    assert not rule.holds(make_context(customer=stale))


def test_dnd_blocks_voice_as_policy_not_as_law():
    """TRAI-06 governs promotional messaging. Antar's voice rule is policy above the
    legal floor, and the register says so rather than dressing it as compliance."""
    rule = reg.get("TRAI-06")
    dnd = CustomerContext(customer_id="c", merchant_id="m", dnd_registered=True)
    assert not rule.holds(make_context(customer=dnd, channel=Channel.VOICE))
    # A transactional SMS to a DND-registered customer is lawful.
    assert rule.holds(make_context(customer=dnd, channel=Channel.SMS))
    assert rule.severity is Severity.ADVISORY
    assert "overstated" in rule.note


def test_silent_retries_skip_contact_only_rules_but_not_debit_rules():
    """A silent retry sends no message, so the contact window cannot apply to it —
    but the AFA ceiling and the mandate state certainly do."""
    at_night = make_context(channel=Channel.SILENT_RETRY, hour=3, hours_ahead=25)
    assert not reg.get("TRAI-01").applies(at_night)
    assert reg.get("RBI-EM-03").applies(at_night)
    assert reg.get("RBI-EM-01").applies(at_night)


# ----------------------------------------------------------------- compiler


def test_compiler_removes_infeasible_candidates_rather_than_constraining_them():
    """A regulation is not something the solver may trade off against the objective."""
    problem = compile_problem([candidate("ok"), candidate("late", hours_ahead=2)])
    assert problem.feasible_ids == {"ok"}
    assert len(problem.rejected) == 1
    assert "RBI-EM-01" in problem.rejected[0].regulation_ids


def test_rejection_is_explainable_in_plain_language():
    problem = compile_problem([candidate("late", hours_ahead=2)])
    explanation = explain_rejection(problem, "late")
    assert "RBI-EM-01" in explanation
    assert "24 hours" in explanation


def test_contact_budget_row_accounts_for_contacts_already_spent():
    spent = CustomerContext(customer_id="cust_1", merchant_id="mer1", contacts_in_window=2)
    problem = compile_problem(
        [Candidate("a", make_context(customer=spent))], contacts_per_30d=3
    )
    row = next(r for r in problem.rows if r.kind is RowKind.CONTACT_BUDGET)
    assert row.bound == 1.0


def test_an_exhausted_budget_rejects_the_candidate_rather_than_bounding_it_at_zero():
    """Two mechanisms, deliberately, and the stricter one wins.

    `POL-BUDGET` is both a per-candidate predicate (has this customer already had
    their three?) and an LP row (how many may be selected in this batch?). A customer
    already over budget fails the predicate and never reaches the solver at all, which
    is stronger than handing the solver a row with a bound of zero and trusting it.
    """
    spent = CustomerContext(customer_id="cust_1", merchant_id="mer1", contacts_in_window=9)
    problem = compile_problem(
        [Candidate("a", make_context(customer=spent))], contacts_per_30d=3
    )
    assert not problem.feasible
    assert "POL-BUDGET" in problem.rejected[0].regulation_ids
    assert not [r for r in problem.rows if r.kind is RowKind.CONTACT_BUDGET]


def test_one_action_per_event_row_appears_only_when_needed():
    single = compile_problem([candidate("a")])
    assert not [r for r in single.rows if r.kind is RowKind.ONE_ACTION_PER_EVENT]

    two = compile_problem([candidate("a"), candidate("b", channel=Channel.WHATSAPP)])
    row = next(r for r in two.rows if r.kind is RowKind.ONE_ACTION_PER_EVENT)
    assert row.bound == 1.0
    assert set(row.coefficients) == {"a", "b"}


def test_margin_budget_row_only_covers_discounted_candidates():
    problem = compile_problem(
        [candidate("plain"), candidate("discounted", discount_paise=10000)],
        margin_budget_paise=50000,
    )
    row = next(r for r in problem.rows if r.kind is RowKind.MARGIN_BUDGET)
    assert set(row.coefficients) == {"discounted"}


def test_silent_retries_do_not_consume_the_contact_budget():
    problem = compile_problem([candidate("silent", channel=Channel.SILENT_RETRY)])
    assert not [r for r in problem.rows if r.kind is RowKind.CONTACT_BUDGET]


def test_advisory_violations_are_flagged_not_blocking():
    dnd = CustomerContext(customer_id="cust_1", merchant_id="mer1", dnd_registered=True)
    problem = compile_problem([Candidate("voice", make_context(customer=dnd, channel=Channel.VOICE))])
    assert problem.feasible_ids == {"voice"}
    assert "TRAI-06" in problem.advisory_flags["voice"]


def test_compiler_is_pure():
    """Same inputs, same output. No clock, no config, no hidden state."""
    candidates = [candidate("a"), candidate("b", hours_ahead=2)]
    first = compile_problem(candidates)
    second = compile_problem(candidates)
    assert first.feasible_ids == second.feasible_ids
    assert [r.row_id for r in first.rows] == [r.row_id for r in second.rows]


# ---------------------------------------------------------------- validator


def test_validator_agrees_with_the_compiler_on_a_feasible_set():
    problem = compile_problem([candidate("a")])
    assert validate(problem.feasible, window_start_hour=10).ok


def test_validator_catches_what_the_compiler_rejected():
    """Both implementations must condemn the same action, by different routes."""
    late = candidate("late", hours_ahead=2)
    problem = compile_problem([late])
    assert not problem.feasible
    result = validate([late], window_start_hour=10)
    assert not result.ok
    assert any(v.rule == "RBI-EM-01" for v in result.violations)


def test_validator_catches_a_budget_breach_the_rows_would_have_allowed():
    """The validator is not derived from the rows, so it can disagree with them."""
    spent = CustomerContext(customer_id="cust_1", merchant_id="mer1", contacts_in_window=3)
    chosen = [Candidate("a", make_context(customer=spent))]
    result = validate(chosen, contacts_per_30d=3, window_start_hour=10)
    assert any(v.rule == "POL-BUDGET" for v in result.violations)


def test_validator_catches_two_actions_on_one_cycle():
    result = validate(
        [candidate("a"), candidate("b", channel=Channel.WHATSAPP)], window_start_hour=10
    )
    assert any(v.rule == "ONE_ACTION_PER_EVENT" for v in result.violations)


def test_validator_catches_a_cooldown_breach_between_two_selected_contacts():
    a = candidate("a", hours_ahead=25)
    b = Candidate("b", make_context(hours_ahead=30, channel=Channel.WHATSAPP))
    result = validate([a, b], cooldown_hours=72, window_start_hour=10)
    assert any(v.rule == "POL-COOLDOWN" for v in result.violations)


def test_validator_reports_are_human_readable():
    result = validate([candidate("late", hours_ahead=1)], window_start_hour=10)
    report = result.report()
    assert "RBI-EM-01" in report and "violation" in report


# --------------------------------------------------------------------- gate


def make_decision(*, amount_paise: int = 49900, discount_paise: int = 0, channel=Channel.SMS):
    context = make_context(amount_paise=amount_paise, discount_paise=discount_paise, channel=channel)
    return Decision(
        decision_id="dec_1",
        event_id="mer1_evt_0001",
        chosen=context.intervention,
        expected_incremental_paise=1000,
        model_version="m1",
        policy_version="p1",
    )


def test_gate_defaults_to_dry_run():
    gate = PolicyGate(ledger=NullLedger())
    record = gate.submit(make_decision(), idempotency_key=idempotency_key("dec_1", 1))
    assert record.dry_run and not record.executed
    assert not record.blocked


def test_gate_requires_an_idempotency_key():
    gate = PolicyGate()
    with pytest.raises(GateRefusal, match="idempotency"):
        gate.submit(make_decision(), idempotency_key="")


def test_replaying_a_key_is_a_no_op_returning_the_original():
    """PLAN.md M4 acceptance."""
    executed: list[str] = []
    gate = PolicyGate(dry_run=False)

    def executor(action: ActionRecord) -> str:
        executed.append(action.action_id)
        return "pay_1"

    key = idempotency_key("dec_1", 1)
    first = gate.submit(make_decision(), idempotency_key=key, executor=executor)
    second = gate.submit(make_decision(), idempotency_key=key, executor=executor)

    assert first == second
    assert len(executed) == 1, "a replayed key must not execute again"


def test_gate_refuses_an_action_without_a_decision():
    gate = PolicyGate()
    with pytest.raises(GateRefusal, match="no chosen intervention"):
        gate.submit(
            Decision(decision_id="dec_2", event_id="mer1_evt_0001", chosen=None),
            idempotency_key=idempotency_key("dec_2", 1),
        )


def test_per_action_cap_blocks():
    gate = PolicyGate(limits=GateLimits(per_action_cap_paise=10_000))
    record = gate.submit(
        make_decision(amount_paise=50_000), idempotency_key=idempotency_key("dec_1", 1),
        amount_paise=50_000,
    )
    assert record.blocked and "per-action cap" in (record.rejected_reason or "")


def test_daily_cap_accumulates():
    gate = PolicyGate(limits=GateLimits(per_day_cap_paise=60_000), dry_run=False)
    for index in range(2):
        gate.submit(
            make_decision(), idempotency_key=idempotency_key("dec_1", index),
            amount_paise=40_000, executor=lambda a: "ref",
        )
    third = gate.submit(
        make_decision(), idempotency_key=idempotency_key("dec_1", 99),
        amount_paise=40_000, executor=lambda a: "ref",
    )
    assert third.blocked and "today's cap" in (third.rejected_reason or "")


def test_human_approval_threshold_blocks_until_an_approver_is_recorded():
    gate = PolicyGate(limits=GateLimits(human_approval_threshold_paise=10_000))
    blocked = gate.submit(
        make_decision(amount_paise=50_000), idempotency_key=idempotency_key("dec_1", 1),
        amount_paise=50_000,
    )
    assert blocked.requires_human_approval and blocked.blocked

    approved = gate.submit(
        make_decision(amount_paise=50_000), idempotency_key=idempotency_key("dec_1", 2),
        amount_paise=50_000, approved_by="ops@merchant.example",
    )
    assert approved.requires_human_approval and not approved.blocked
    assert approved.approved_by == "ops@merchant.example"


def test_gate_blocks_a_promotional_draft():
    """TRAI-03 at the choke point, independent of the contamination detector."""
    gate = PolicyGate()
    draft = DraftedMessage(
        template_id="t1", message_class=MessageClass.PROMOTIONAL,
        slots={}, rendered="Pay now. Also, 20% off annual plans!",
    )
    record = gate.submit(
        make_decision(), idempotency_key=idempotency_key("dec_1", 1), draft=draft
    )
    assert record.blocked and "TRAI-03" in (record.rejected_reason or "")


def test_gate_blocks_a_contaminated_draft_even_when_the_class_looks_right():
    gate = PolicyGate()
    draft = DraftedMessage(
        template_id="t1", message_class=MessageClass.TRANSACTIONAL,
        slots={}, rendered="Your payment failed. Upgrade today and save!",
        contamination_score=0.91,
    )
    record = gate.submit(
        make_decision(), idempotency_key=idempotency_key("dec_1", 1), draft=draft
    )
    assert record.blocked and "contamination" in (record.rejected_reason or "")


def test_the_gate_reads_the_amount_from_the_decision_not_the_draft():
    """N1, mechanically. A model emitting a discount field emits into the void."""
    gate = PolicyGate()
    draft = DraftedMessage(
        template_id="t1", message_class=MessageClass.TRANSACTIONAL,
        slots={"discount": "5000", "amount": "999999"},
        rendered="Pay Rs 9,999,99 with 5000 off",
    )
    record = gate.submit(
        make_decision(amount_paise=49900), idempotency_key=idempotency_key("dec_1", 1),
        amount_paise=49900, draft=draft,
    )
    assert record.amount_paise == 49900
    assert record.discount_paise == 0


def test_discount_may_not_exceed_the_amount():
    gate = PolicyGate()
    record = gate.submit(
        make_decision(amount_paise=10_000, discount_paise=20_000),
        idempotency_key=idempotency_key("dec_1", 1), amount_paise=10_000,
    )
    assert record.blocked and "discount exceeds" in (record.rejected_reason or "")


def test_blocked_actions_still_reach_the_ledger():
    """A refusal is evidence. It must be recorded, not dropped."""
    ledger = NullLedger()
    gate = PolicyGate(ledger=ledger, limits=GateLimits(per_action_cap_paise=1))
    gate.submit(
        make_decision(), idempotency_key=idempotency_key("dec_1", 1), amount_paise=50_000
    )
    assert len(ledger.actions) == 1
    assert ledger.actions[0].blocked


def test_there_is_no_bypass_flag_on_the_gate():
    """N2: 'no bypass path exists in the codebase'."""
    forbidden = {"enabled", "disabled", "skip_checks", "bypass", "force", "unsafe"}
    attributes = set(vars(PolicyGate())) | set(dir(PolicyGate))
    assert not (forbidden & attributes), f"gate exposes a bypass: {forbidden & attributes}"


# ------------------------------------------------------------ requires_gate


def test_requires_gate_refuses_an_ungated_call():
    @requires_gate
    def send(action: ActionRecord) -> str:
        return "sent"

    with pytest.raises(GateRefusal, match="without an ActionRecord"):
        send(None)  # type: ignore[arg-type]


def test_requires_gate_refuses_a_refused_action():
    @requires_gate
    def send(action: ActionRecord) -> str:
        return "sent"

    refused = ActionRecord(
        action_id="act_1", decision_id="dec_1", event_id="evt_1",
        idempotency_key="k", channel=Channel.SMS, message_class=MessageClass.TRANSACTIONAL,
        rejected_reason="capped",
    )
    with pytest.raises(GateRefusal, match="the gate refused"):
        send(refused)


def test_requires_gate_allows_an_approved_action():
    @requires_gate
    def send(action: ActionRecord) -> str:
        return "sent"

    approved = ActionRecord(
        action_id="act_1", decision_id="dec_1", event_id="evt_1",
        idempotency_key="k", channel=Channel.SMS, message_class=MessageClass.TRANSACTIONAL,
    )
    assert send(approved) == "sent"
    assert send.__antar_requires_gate__ is True


# ------------------------------------------------------------------ budgets


def test_contact_ledger_counts_a_rolling_window_not_a_calendar_month():
    ledger = ContactLedger(policy=BudgetPolicy(contacts_per_30d=3))
    ledger.record("cust_1", NOW - timedelta(days=29))
    ledger.record("cust_1", NOW - timedelta(days=31))
    assert ledger.contacts_in_window("cust_1", as_of=NOW) == 1
    assert ledger.remaining("cust_1", as_of=NOW) == 2


def test_hydrate_fills_budget_fields_in_one_place():
    ledger = ContactLedger()
    ledger.record("cust_1", NOW - timedelta(hours=10))
    customer = CustomerContext(customer_id="cust_1", merchant_id="mer1")
    hydrated = ledger.hydrate(customer, as_of=NOW)
    assert hydrated.contacts_in_window == 1
    assert hydrated.last_contact_at == NOW - timedelta(hours=10)


def test_margin_budget_scales_with_the_batch():
    policy = BudgetPolicy(margin_budget_fraction=0.02)
    assert policy.margin_budget_paise(10_000_000) == 200_000
