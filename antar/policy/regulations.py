"""Regulation as data. **N5: never hard-coded inline in business logic.**

Every rule Antar obeys lives here as a `Regulation` with a predicate, a citation, and
a verification status. `antar/policy/compiler.py` turns them into LP constraint rows;
`docs/REGULATORY_REGISTER.md` is generated from this file so code and document cannot
drift apart.

## Verification

PLAN.md section 3 imposes a duty: rules compiled from secondary sources must be
re-checked against the primary document before the final commit, and anything that
cannot be verified is downgraded to `ADVISORY` and labelled. That check was done at
*encoding* time rather than at submission time, because a threshold that turns out to
differ changes the constraint rows and everything built on them.

It was worth doing. **It found a real error in our own specification** - see
`TRAI-01`. PLAN.md section 3.2 stated the commercial-contact window as 09:00-21:00.
The gazette text says the 08:00-10:00 band is *default OFF* for every customer
regardless of registration, so the default-permitted window opens at **10:00**, not
09:00. Antar loses an hour of contact capacity it thought it had.

`test_blocking_rules_are_verified` enforces the rule mechanically: a regulation may
only be `BLOCKING` if its verification status is `PRIMARY` or `REPRODUCTION`. A rule
resting on a law-firm summary cannot block money.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from antar import clock
from antar.signals.schemas import (
    CONTACT_CHANNELS,
    Channel,
    ConsentBasis,
    DecisionContext,
    MandateState,
    MessageClass,
)


class Severity(StrEnum):
    BLOCKING = "BLOCKING"
    """Violating this stops the action. Compiled into a hard LP constraint."""

    ADVISORY = "ADVISORY"
    """Reported and priced, but does not block. Used for rules we could not verify
    against a primary source, and for obligations that fall on someone other than us."""


class Verification(StrEnum):
    PRIMARY = "PRIMARY"
    """Checked against the regulator's own published document."""

    REPRODUCTION = "REPRODUCTION"
    """Checked against a full-text reproduction of the gazette (e.g. Indian Kanoon)
    rather than the regulator's PDF. The text is the text; the provenance is one step
    removed."""

    SECONDARY = "SECONDARY"
    """Law-firm note, trade press, or vendor documentation. **May not be BLOCKING.**"""

    UNVERIFIED = "UNVERIFIED"
    """Asserted in PLAN.md and not yet checked. **May not be BLOCKING.**"""


VERIFIABLE_ENOUGH_TO_BLOCK: frozenset[Verification] = frozenset(
    {Verification.PRIMARY, Verification.REPRODUCTION}
)


@dataclass(frozen=True)
class Regulation:
    """One rule, as data.

    `predicate` returns True when the context is **compliant**. Predicates are pure
    functions of `DecisionContext`, which is what lets the constraint compiler turn
    them into LP rows and the property tests generate contexts at random.
    """

    id: str
    title: str
    source: str
    citation_url: str
    clause: str
    effective_date: date
    severity: Severity
    verification: Verification
    human_explanation: str
    predicate: Callable[[DecisionContext], bool]
    quote: str = ""
    """Verbatim text where we have it. Empty means we are paraphrasing."""

    note: str = ""
    """Discrepancies, corrections, and anything a reader should distrust."""

    verified_on: date | None = None
    applies_to_contacts_only: bool = True
    """Most rules govern outbound communication. A few (the AFA ceilings, the mandate
    state machine) govern the debit itself and apply to a silent retry too."""

    def __post_init__(self) -> None:
        if self.severity is Severity.BLOCKING and self.verification not in VERIFIABLE_ENOUGH_TO_BLOCK:
            raise ValueError(
                f"{self.id} is BLOCKING but only {self.verification.value}-verified. "
                "A rule resting on a secondary source may not block money; downgrade "
                "it to ADVISORY and say so in the register."
            )
        if not self.citation_url:
            raise ValueError(f"{self.id} has no citation_url")

    def holds(self, context: DecisionContext) -> bool:
        return bool(self.predicate(context))

    def applies(self, context: DecisionContext) -> bool:
        if not self.applies_to_contacts_only:
            return True
        return context.intervention.channel in CONTACT_CHANNELS

    def violated_by(self, context: DecisionContext) -> bool:
        return self.applies(context) and not self.holds(context)


# ---------------------------------------------------------------------------
# Predicates. Written as named functions rather than lambdas so that a failing
# property test names the rule that broke rather than "<lambda>".
# ---------------------------------------------------------------------------


def _lead_time_ok(ctx: DecisionContext) -> bool:
    """RBI-EM-01. The notification must precede the debit by at least 24 hours,
    so the decision has to be committed at least that far ahead."""
    return ctx.intervention.scheduled_for - ctx.now >= timedelta(hours=ctx.lead_time_hours)


def _optout_respected(ctx: DecisionContext) -> bool:
    """RBI-EM-02. An exercised opt-out is absolute; there is no grace window."""
    return not ctx.customer.optout_received


def _afa_routed(ctx: DecisionContext) -> bool:
    """RBI-EM-03 / RBI-EM-04. Above the category ceiling the debit needs an
    additional factor, so it must be routed through an AFA-bearing flow."""
    if ctx.event.amount_paise <= ctx.afa_free_ceiling_paise:
        return True
    return ctx.intervention.requires_afa


def _mandate_is_live(ctx: DecisionContext) -> bool:
    """RBI-EM-06. A paused or withdrawn mandate is not a retry candidate."""
    return ctx.customer.mandate_state in (MandateState.ACTIVE, MandateState.HALTED)


def _inside_contact_window(ctx: DecisionContext) -> bool:
    """TRAI-01. Default-OFF time bands, honoured for every customer."""
    return clock.in_contact_window(
        ctx.intervention.scheduled_for,
        start_hour=ctx.contact_window_start_hour,
        end_hour=ctx.contact_window_end_hour,
    )


def _message_class_declared(ctx: DecisionContext) -> bool:
    """TRAI-02. Every outbound action carries an explicit class."""
    return ctx.intervention.message_class in (MessageClass.TRANSACTIONAL, MessageClass.SERVICE)


def _not_promotional(ctx: DecisionContext) -> bool:
    """TRAI-03. Promotional content anywhere in the message reclassifies all of it."""
    return ctx.intervention.message_class is not MessageClass.PROMOTIONAL


def _consent_live(ctx: DecisionContext) -> bool:
    """TRAI-04. Explicit consent expires; inferred consent lives with the contract."""
    customer = ctx.customer
    if customer.consent_basis is ConsentBasis.NONE:
        return False
    if customer.consent_basis is ConsentBasis.INFERRED:
        # Valid for the duration of the contractual relationship. An active or
        # halted mandate *is* that relationship.
        return customer.mandate_state not in (MandateState.REVOKED, MandateState.COMPLETED)
    if customer.consent_granted_at is None:
        return False
    age = ctx.now - customer.consent_granted_at
    return age <= timedelta(days=ctx.explicit_consent_validity_days)


def _dnd_respected(ctx: DecisionContext) -> bool:
    """TRAI-06, as actually written *plus* a merchant-policy choice.

    The regulation blocks *promotional* communication to a preference-registered
    customer; transactional and service messages are not caught by DND. Antar never
    sends promotional recovery messages, so the legal floor is vacuous here.

    We nonetheless decline voice calls to DND-registered customers. That is a policy
    choice above the legal minimum, not a legal requirement, and it is labelled as
    such rather than dressed up as compliance. A dunning phone call to someone who
    registered a preference against being called is the kind of thing that is legal
    and still wrong.
    """
    if ctx.intervention.message_class is MessageClass.PROMOTIONAL:
        return not ctx.customer.dnd_registered
    voice_to_registered = (
        ctx.intervention.channel is Channel.VOICE and ctx.customer.dnd_registered
    )
    return not voice_to_registered


def _promise_to_pay_respected(ctx: DecisionContext) -> bool:
    """Merchant policy, not regulation. Chasing someone who has already promised to
    pay is how a recovery programme generates complaints."""
    return ctx.customer.promise_to_pay_at is None


def _within_contact_budget(ctx: DecisionContext) -> bool:
    """C-BUDGET. Merchant policy."""
    return ctx.customer.contacts_in_window < ctx.contacts_allowed_per_30d


def _cooldown_elapsed(ctx: DecisionContext) -> bool:
    """C-COOLDOWN. Merchant policy."""
    if ctx.customer.last_contact_at is None:
        return True
    gap = ctx.intervention.scheduled_for - ctx.customer.last_contact_at
    return gap >= timedelta(hours=ctx.cooldown_hours)


# ---------------------------------------------------------------------------
# The register.
# ---------------------------------------------------------------------------

RBI_URL = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374"
TRAI_AMENDMENT_URL = (
    "https://www.trai.gov.in/telecom-commercial-communications-customer-preference"
    "-second-amendment-regulations-2025"
)
TRAI_GAZETTE_PDF = "https://www.trai.gov.in/sites/default/files/2025-02/Regulation_12022025.pdf"
TCCCPR_FULLTEXT = "https://indiankanoon.org/doc/60694660/"

VERIFIED_ON = date(2026, 8, 22)

REGULATIONS: tuple[Regulation, ...] = (
    # ---------------------------------------------------------------- RBI
    Regulation(
        id="RBI-EM-01",
        title="Pre-transaction notification at least 24 hours before the debit",
        source="RBI Digital Payments - E-mandate Framework, 2026 (RBI/DPSS/2026-27/396)",
        citation_url=RBI_URL,
        clause="Section 6(a)",
        quote=(
            "An issuer shall send a pre-transaction notification to the customer, at "
            "least 24 hours prior to the actual charge / debit."
        ),
        effective_date=date(2026, 4, 21),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "Every debit attempt must be announced 24 hours in advance. For Antar this "
            "is a lead-time constraint on the scheduler, not a reminder: a retry "
            "decided now cannot execute until tomorrow, so the decision horizon is at "
            "least a day and the opportunity to act on fresh information is limited."
        ),
        predicate=_lead_time_ok,
    ),
    Regulation(
        id="RBI-EM-02",
        title="Per-transaction opt-out facility, validated by AFA",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 6(c)",
        quote=(
            "The issuer shall provider a customer with a facility to opt-out of any "
            "particular transaction or the e-mandate. Any such opt-out shall be "
            "validated by the issuer using AFA."
        ),
        effective_date=date(2026, 4, 21),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "The mandated notification carries a cancel button. This is the single "
            "most consequential fact in Antar's design: every retry we schedule "
            "generates a notification, and every notification is an opportunity for "
            "the customer to leave. Outreach is therefore not free even when it is "
            "free - it carries a churn hazard that the allocator has to price. Once an "
            "opt-out has been exercised it is absolute, with no grace window."
        ),
        note=(
            "The 'provider' typo is in the source text and is reproduced verbatim "
            "rather than silently corrected."
        ),
        predicate=_optout_respected,
    ),
    Regulation(
        id="RBI-EM-03",
        title="AFA-free ceiling of Rs 15,000 per recurring transaction",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 8(a)",
        quote="All recurring transactions may be authorised without AFA up to Rs 15,000/- per transaction.",
        effective_date=date(2026, 4, 21),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "Above Rs 15,000 the customer must authenticate every time. Recovery above "
            "the ceiling has to route through an AFA-bearing flow, which completes "
            "materially less often - so the amount is not just a payoff, it is a "
            "difficulty. The allocator must model that, or it will over-value large "
            "cycles."
        ),
        predicate=_afa_routed,
    ),
    Regulation(
        id="RBI-EM-04",
        title="Rs 1,00,000 ceiling for insurance, mutual funds and credit card bills",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 8(b)",
        quote=(
            "Payment of insurance premiums, subscription to mutual funds, and credit "
            "card bill payments may be made without AFA up to Rs 1,00,000/- per "
            "transaction."
        ),
        effective_date=date(2026, 4, 21),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "The ceiling is category-dependent, not global. Merchant category is "
            "therefore a genuine feature rather than a label: the same Rs 40,000 debit "
            "is AFA-free for an insurer and AFA-bearing for a streaming service."
        ),
        predicate=_afa_routed,
    ),
    Regulation(
        id="RBI-EM-05",
        title="Post-transaction notification after every debit",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 7",
        effective_date=date(2026, 4, 21),
        severity=Severity.ADVISORY,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "A fixed cost per successful debit, carried in the cost model rather than "
            "as a constraint. ADVISORY because the obligation falls on the issuer, not "
            "on Antar: we cannot violate it, we can only pay for it."
        ),
        predicate=lambda _ctx: True,
    ),
    Regulation(
        id="RBI-EM-06",
        title="Customer may modify or withdraw a mandate at any time",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 4(b), 4(e)",
        quote=(
            "modify the validity period or withdraw the e-mandate at any point of time"
        ),
        effective_date=date(2026, 4, 21),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "A withdrawn mandate is not a retry candidate, and neither is a paused "
            "one. Debiting either is not a bug in our retry logic - it is a debit the "
            "customer told us not to make."
        ),
        note=(
            "PLAN.md section 3.1 rendered this as 'modify, pause, or withdraw'. The "
            "framework text says 'modify the validity period or withdraw'; **pause is "
            "not a term the framework uses**. PAUSED survives in our mandate FSM "
            "because it is a real Razorpay subscription state, but it is a payment-"
            "processor lifecycle state rather than an RBI-conferred right, and the "
            "register should not imply otherwise."
        ),
        predicate=_mandate_is_live,
    ),
    Regulation(
        id="RBI-EM-07",
        title="FASTag and NCMC auto-replenishment exempt from pre-debit notification",
        source="RBI Digital Payments - E-mandate Framework, 2026",
        citation_url=RBI_URL,
        clause="Section 6(d)",
        quote=(
            "Pre-transaction notification is not required for e-mandates registered to "
            "auto-replenish balances of FASTag, and National Common Mobility Card (NCMC)."
        ),
        effective_date=date(2026, 4, 21),
        severity=Severity.ADVISORY,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        applies_to_contacts_only=False,
        human_explanation=(
            "Out of scope: Antar handles no FASTag or NCMC mandates. Encoded anyway so "
            "that the exemption is visibly considered and excluded rather than "
            "overlooked. If those mandates were ever in scope they would be the only "
            "population without the opt-out hazard, and the economics would differ."
        ),
        predicate=lambda _ctx: True,
    ),
    # ---------------------------------------------------------------- TRAI
    Regulation(
        id="TRAI-01",
        title="Commercial communication permitted only between 10:00 and 21:00",
        source="TRAI TCCCPR 2018, Schedule II (as amended 12 Feb 2025)",
        citation_url=TCCCPR_FULLTEXT,
        clause="Schedule II, para 3(1) Note-1",
        quote=(
            "Time Bands (i), (ii), (iii) and (ix) shall be default OFF for all "
            "customers irrespective of the status of registration of customer i.e. for "
            "all customers including those who have not registered any type of "
            "preference(s), anytime unless customer has registered its preference(s) "
            "and switched ON"
        ),
        effective_date=date(2018, 7, 19),
        severity=Severity.BLOCKING,
        verification=Verification.REPRODUCTION,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Bands (i) 00:00-06:00, (ii) 06:00-08:00, (iii) 08:00-10:00 and "
            "(ix) 21:00-24:00 are OFF by default for every customer, registered or "
            "not. The default-permitted window is therefore 10:00-21:00 - eleven "
            "hours, not twelve."
        ),
        note=(
            "**This corrects PLAN.md section 3.2**, which stated the window as "
            "09:00-21:00 from secondary sources. The 08:00-10:00 band is default OFF, "
            "so the window opens at 10:00. Antar has one hour per day less contact "
            "capacity than the plan assumed, which raises the shadow price on a "
            "contact slot rather than lowering it. The plan also said the restriction "
            "applies 'every day including weekends and public holidays'; the gazette "
            "treats public and national holidays as a *customer-registrable "
            "preference* (Schedule II, para 4(viii)), not a blanket prohibition - see "
            "TRAI-08. Ultimate authority is the gazette PDF, not this reproduction."
        ),
        predicate=_inside_contact_window,
    ),
    Regulation(
        id="TRAI-02",
        title="Every commercial communication carries an explicit class",
        source="TRAI TCCCPR 2018 as amended",
        citation_url=TRAI_AMENDMENT_URL,
        clause="Regulation 2(1) definitions; Second Amendment 2025",
        effective_date=date(2025, 2, 12),
        severity=Severity.ADVISORY,
        verification=Verification.SECONDARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Communications are classified service / transactional / promotional (and, "
            "since the Second Amendment, government), each requiring the correct "
            "registered header and number series. Antar attaches the class to every "
            "outbound action and refuses to construct a promotional one."
        ),
        note=(
            "ADVISORY: the classification scheme is confirmed by law-firm commentary "
            "on the Second Amendment, but we have not read the definitional clause in "
            "the gazette. Per PLAN.md section 3 an unverified rule may not block."
        ),
        predicate=_message_class_declared,
    ),
    Regulation(
        id="TRAI-03",
        title="Promotional content contaminates the whole message",
        source="TRAI TCCCPR (Second Amendment) Regulations, 2025",
        citation_url=TRAI_AMENDMENT_URL,
        clause="Second Amendment, 12 February 2025",
        quote=(
            "if promotional content is mixed with any other type of communication "
            "(say, service or transactional) then such communication will be treated "
            "as promotional"
        ),
        effective_date=date(2025, 2, 12),
        severity=Severity.BLOCKING,
        verification=Verification.REPRODUCTION,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "One upsell sentence appended to a dunning message reclassifies the entire "
            "message as promotional, which changes the header, the number series, and "
            "whether DND applies. This is the rule the LLM most plausibly breaks - it "
            "is trained to be helpful, and 'we also have 20% off annual plans' is a "
            "helpful sentence. `antar/act/contamination.py` is the defence, and it "
            "runs on the model's output before the gate ever sees it."
        ),
        predicate=_not_promotional,
    ),
    Regulation(
        id="TRAI-04",
        title="Inferred consent lasts only for the contract; explicit consent expires",
        source="TRAI TCCCPR (Second Amendment) Regulations, 2025",
        citation_url=TRAI_AMENDMENT_URL,
        clause="Second Amendment, 12 February 2025",
        quote=(
            "inferred consent remains valid only for the duration of a contractual "
            "relationship. In other words, once the contract ends, inferred consent is "
            "automatically revoked."
        ),
        effective_date=date(2025, 2, 12),
        severity=Severity.BLOCKING,
        verification=Verification.REPRODUCTION,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "A live mandate is the contractual relationship, so inferred consent covers "
            "recovery messaging while the mandate is live and stops the moment it is "
            "revoked. This is why TERMINATE means terminate: after revocation there is "
            "no consent basis left to message on."
        ),
        note=(
            "The seven-day explicit-consent validity window in PLAN.md section 3.2 is "
            "reported by secondary sources and is **not** confirmed here. The "
            "configured value is used only for customers on an EXPLICIT basis, of "
            "which the simulator generates none, so nothing measured depends on it."
        ),
        predicate=_consent_live,
    ),
    Regulation(
        id="TRAI-05",
        title="Number series must match the communication class",
        source="TRAI TCCCPR 2018 as amended; NCPR number-series allocation",
        citation_url=TRAI_AMENDMENT_URL,
        clause="Second Amendment 2025; 1600-series mandate for BFSI from 1 Jan 2026",
        effective_date=date(2026, 1, 1),
        severity=Severity.ADVISORY,
        verification=Verification.SECONDARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Promotional voice uses the 140 series; transactional and service calls use "
            "the 1600/160 series. Channel identity is part of the action record and is "
            "validated against the message class."
        ),
        note=(
            "ADVISORY: confirmed only by trade press and vendor documentation. Antar "
            "does not place real calls, so no number series is ever allocated and the "
            "rule cannot be exercised end to end."
        ),
        predicate=lambda _ctx: True,
    ),
    Regulation(
        id="TRAI-06",
        title="Preference registration (DND) blocks promotional communication",
        source="TRAI TCCCPR 2018 as amended",
        citation_url=TRAI_AMENDMENT_URL,
        clause="Regulation 20-21; Schedule II preference categories",
        effective_date=date(2018, 7, 19),
        severity=Severity.ADVISORY,
        verification=Verification.SECONDARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "DND governs *promotional* communication. Transactional and service "
            "messages are not caught by it, so a recovery message to a DND-registered "
            "customer is lawful. Antar declines voice calls to them anyway."
        ),
        note=(
            "**PLAN.md section 3.2 overstated this** as an unconditional blocking "
            "predicate on all contact. As written that would be wrong law - it would "
            "block lawful transactional messaging - so the predicate encodes the "
            "actual rule plus an explicitly-labelled merchant-policy choice to avoid "
            "voice calls to preference-registered customers. Policy above the legal "
            "floor is fine; policy *presented as* the legal floor is not."
        ),
        predicate=_dnd_respected,
    ),
    Regulation(
        id="TRAI-07",
        title="Auto-dialer and robocall use must be declared to the access provider",
        source="TRAI TCCCPR (Second Amendment) Regulations, 2025",
        citation_url=TRAI_GAZETTE_PDF,
        clause="Second Amendment, 12 February 2025",
        quote=(
            "provisions concerning autodialers and robocalls have been introduced, "
            "requiring senders to notify the Originating Access Provider (OAP) of "
            "their intent and purpose to use such means"
        ),
        effective_date=date(2025, 2, 12),
        severity=Severity.ADVISORY,
        verification=Verification.REPRODUCTION,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "The disclosure runs to the originating access provider, as a registration "
            "obligation, rather than to the customer inside the call."
        ),
        note=(
            "**Corrects PLAN.md section 3.2**, which read this as a customer-facing "
            "disclosure ('every synthetic voice action carries a disclosure flag'). "
            "It is an operator-facing registration duty. Antar keeps the flag on the "
            "action record for auditability, but describing it as a consumer "
            "disclosure would have been wrong."
        ),
        predicate=lambda _ctx: True,
    ),
    Regulation(
        id="TRAI-08",
        title="Customer-registered preferences narrow the window further",
        source="TRAI TCCCPR 2018, Schedule II",
        citation_url=TCCCPR_FULLTEXT,
        clause="Schedule II, paras 3-4",
        effective_date=date(2018, 7, 19),
        severity=Severity.ADVISORY,
        verification=Verification.REPRODUCTION,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Beyond the default-OFF bands, a customer may register preferences on "
            "specific two-hour bands, on days of the week, and on public and national "
            "holidays. Those narrow the permitted window below the 10:00-21:00 default."
        ),
        note=(
            "ADVISORY because Antar has no preference data to honour: the simulator "
            "generates no per-customer time-band registrations, so this rule can never "
            "bind and reporting it as BLOCKING would be claiming a compliance check we "
            "do not perform. **A production deployment must consume the preference "
            "feed before this can be called compliant.** docs/LIMITATIONS.md L11."
        ),
        predicate=lambda _ctx: True,
    ),
    # -------------------------------------------------- merchant policy
    Regulation(
        id="POL-BUDGET",
        title="At most k contacts per customer per rolling 30 days",
        source="Merchant policy (config: budgets.contacts_per_30d)",
        citation_url="internal://config/default.yaml#budgets.contacts_per_30d",
        clause="C-BUDGET",
        effective_date=date(2026, 8, 22),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Not a legal limit but the scarce resource the whole allocator exists to "
            "ration. Its dual is the headline shadow price: what one more compliant "
            "contact slot is worth to this merchant."
        ),
        predicate=_within_contact_budget,
    ),
    Regulation(
        id="POL-COOLDOWN",
        title="Minimum gap between contacts to the same customer",
        source="Merchant policy (config: budgets.cooldown_hours)",
        citation_url="internal://config/default.yaml#budgets.cooldown_hours",
        clause="C-COOLDOWN",
        effective_date=date(2026, 8, 22),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "Three messages inside the budget is compliant and still harassment if they "
            "arrive on consecutive days. Notification fatigue also compounds the "
            "opt-out hazard, so the cooldown is defensive as well as decent."
        ),
        predicate=_cooldown_elapsed,
    ),
    Regulation(
        id="POL-PROMISE",
        title="Stop contacting a customer who has promised to pay",
        source="Merchant policy",
        citation_url="internal://docs/REGULATORY_REGISTER.md#POL-PROMISE",
        clause="C-STOP",
        effective_date=date(2026, 8, 22),
        severity=Severity.BLOCKING,
        verification=Verification.PRIMARY,
        verified_on=VERIFIED_ON,
        human_explanation=(
            "A recorded promise to pay ends the conversation until the promised date. "
            "Chasing past it is the fastest way to convert a paying customer into a "
            "complaint."
        ),
        predicate=_promise_to_pay_respected,
    ),
)


BY_ID: dict[str, Regulation] = {rule.id: rule for rule in REGULATIONS}
BLOCKING: tuple[Regulation, ...] = tuple(r for r in REGULATIONS if r.severity is Severity.BLOCKING)
ADVISORY: tuple[Regulation, ...] = tuple(r for r in REGULATIONS if r.severity is Severity.ADVISORY)


def get(regulation_id: str) -> Regulation:
    return BY_ID[regulation_id]


def evaluate(context: DecisionContext) -> list[str]:
    """Ids of every BLOCKING regulation this context violates. Empty means compliant."""
    return [rule.id for rule in BLOCKING if rule.violated_by(context)]


def evaluate_advisory(context: DecisionContext) -> list[str]:
    return [rule.id for rule in ADVISORY if rule.violated_by(context)]


def is_compliant(context: DecisionContext) -> bool:
    return not evaluate(context)


def iter_regulations() -> Iterator[Regulation]:
    return iter(REGULATIONS)


@dataclass(frozen=True)
class RegisterSummary:
    """Counts for the README and the console, so nobody types them by hand."""

    total: int
    blocking: int
    advisory: int
    primary: int
    reproduction: int
    secondary: int
    unverified: int
    corrections: tuple[str, ...] = field(default_factory=tuple)


def summarise() -> RegisterSummary:
    counts = {v: sum(1 for r in REGULATIONS if r.verification is v) for v in Verification}
    return RegisterSummary(
        total=len(REGULATIONS),
        blocking=len(BLOCKING),
        advisory=len(ADVISORY),
        primary=counts[Verification.PRIMARY],
        reproduction=counts[Verification.REPRODUCTION],
        secondary=counts[Verification.SECONDARY],
        unverified=counts[Verification.UNVERIFIED],
        # Any rule whose note refers back to the plan is a place where verification
        # against the primary source changed what we believed. Matched on the mention
        # rather than on a fixed phrase, because a narrower match silently missed
        # three of the four the first time it ran.
        corrections=tuple(r.id for r in REGULATIONS if "PLAN.md" in r.note),
    )
