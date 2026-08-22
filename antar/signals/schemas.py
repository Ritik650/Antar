"""The contracts between layers (PLAN.md section 7).

Changing a model in this file is a breaking change and needs an ADR in
docs/DECISIONS.md. Every money amount is an integer number of paise; there is no
float rupee anywhere in Antar, because a float rupee eventually becomes a wrong
rupee.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class LossClass(StrEnum):
    MANDATE_FAILURE = "MANDATE_FAILURE"
    CHECKOUT_ABANDON = "CHECKOUT_ABANDON"


class MerchantCategory(StrEnum):
    """Drives the RBI-EM-04 AFA-free ceiling. Not cosmetic - it is a threshold key."""

    OTT_SUBSCRIPTION = "OTT_SUBSCRIPTION"
    UTILITY = "UTILITY"
    EDUCATION = "EDUCATION"
    GENERAL = "GENERAL"
    # The three categories with the Rs 1,00,000 ceiling.
    INSURANCE = "INSURANCE"
    MUTUAL_FUND = "MUTUAL_FUND"
    CREDIT_CARD_BILL = "CREDIT_CARD_BILL"


HIGH_CEILING_CATEGORIES: frozenset[MerchantCategory] = frozenset(
    {
        MerchantCategory.INSURANCE,
        MerchantCategory.MUTUAL_FUND,
        MerchantCategory.CREDIT_CARD_BILL,
    }
)


class PaymentMethod(StrEnum):
    UPI_AUTOPAY = "UPI_AUTOPAY"
    CARD = "CARD"
    ENACH = "ENACH"


class FailureClass(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ISSUER_DOWN = "ISSUER_DOWN"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    AFA_REQUIRED = "AFA_REQUIRED"
    TECHNICAL_DECLINE = "TECHNICAL_DECLINE"
    RISK_DECLINE = "RISK_DECLINE"
    UNKNOWN = "UNKNOWN"


class InterventionClass(StrEnum):
    """What L2 recommends L3 consider. L3 is free to disagree; L2 cannot bind it."""

    WAIT = "WAIT"
    CONTACT = "CONTACT"
    RESCHEDULE = "RESCHEDULE"
    TERMINATE = "TERMINATE"


class Channel(StrEnum):
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    VOICE = "VOICE"
    EMAIL = "EMAIL"
    SILENT_RETRY = "SILENT_RETRY"


CONTACT_CHANNELS: frozenset[Channel] = frozenset(
    {Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL}
)


class MessageClass(StrEnum):
    """TRAI-02. Misclassification is a blocking violation, not a warning."""

    TRANSACTIONAL = "TRANSACTIONAL"
    SERVICE = "SERVICE"
    PROMOTIONAL = "PROMOTIONAL"


class MandateState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    REVOKED = "REVOKED"
    COMPLETED = "COMPLETED"


TERMINAL_MANDATE_STATES: frozenset[MandateState] = frozenset(
    {MandateState.REVOKED, MandateState.COMPLETED}
)


class SegmentHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADING = "DEGRADING"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


class DowntimeSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DowntimeStatus(StrEnum):
    SCHEDULED = "scheduled"
    STARTED = "started"
    RESOLVED = "resolved"


class Arm(StrEnum):
    CONTROL = "CONTROL"
    TREATMENT = "TREATMENT"


class LedgerKind(StrEnum):
    EVENT = "EVENT"
    DIAGNOSIS = "DIAGNOSIS"
    DECISION = "DECISION"
    ACTION = "ACTION"
    OUTCOME = "OUTCOME"
    ALERT = "ALERT"


class ConsentBasis(StrEnum):
    """TRAI-04. Explicit consent expires; inferred consent lives with the contract."""

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"
    NONE = "NONE"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Frozen(BaseModel):
    """Immutable by default. These records are evidence; they do not get edited."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


# ---------------------------------------------------------------------------
# L1 - signals
# ---------------------------------------------------------------------------


class DowntimeWindow(Frozen):
    """Shaped after the Razorpay Downtime entity (payment.downtime.* webhooks)."""

    downtime_id: str
    method: PaymentMethod
    issuer: str | None = None
    psp: str | None = None
    severity: DowntimeSeverity
    status: DowntimeStatus
    begin: datetime
    end: datetime | None = None
    scheduled: bool = False

    def covers(self, when: datetime, *, tolerance_minutes: int = 0) -> bool:
        from datetime import timedelta

        pad = timedelta(minutes=tolerance_minutes)
        if when < self.begin - pad:
            return False
        if self.end is None:
            return True
        return when <= self.end + pad

    def affects(self, method: PaymentMethod, issuer: str | None) -> bool:
        if self.method != method:
            return False
        return not (self.issuer is not None and issuer is not None and self.issuer != issuer)


class SegmentHealthReport(Frozen):
    """Output of the EWMA/CUSUM detector for one issuer x method segment."""

    segment_key: str
    state: SegmentHealth
    ewma_success_rate: float
    baseline_success_rate: float
    cusum: float
    observations: int
    evaluated_at: datetime


class CustomerContext(Frozen):
    """Everything the policy layer needs to know about a person, and nothing more.

    Deliberately does not carry any simulator latent. See
    tests/statistical/test_no_leakage.py - the latents are the answer key.
    """

    customer_id: str
    merchant_id: str
    consent_basis: ConsentBasis = ConsentBasis.INFERRED
    consent_granted_at: datetime | None = None
    dnd_registered: bool = False
    optout_received: bool = False
    promise_to_pay_at: datetime | None = None
    mandate_state: MandateState = MandateState.ACTIVE
    contacts_in_window: int = 0
    last_contact_at: datetime | None = None
    attempts_on_event: int = 0
    tenure_months: int = 0
    timezone_offset_minutes: int = 330  # IST; every simulated customer is in India


class AtRiskEvent(Frozen):
    event_id: str
    merchant_id: str
    customer_id: str
    loss_class: LossClass
    subscription_id: str | None = None
    cycle_number: int | None = None
    amount_paise: int = Field(ge=0)
    merchant_category: MerchantCategory
    method: PaymentMethod
    issuer: str
    occurred_at: datetime
    # Razorpay error fields, verbatim from the payload.
    error_code: str | None = None
    error_reason: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_description: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def segment_key(self) -> str:
        return f"{self.issuer}:{self.method.value}"

    @field_validator("amount_paise")
    @classmethod
    def _whole_paise(cls, v: int) -> int:
        if v % 1 != 0:
            raise ValueError("amount must be a whole number of paise")
        return v


# ---------------------------------------------------------------------------
# L2 - detect
# ---------------------------------------------------------------------------


class EvidenceItem(Frozen):
    """One reason the diagnosis says what it says. Assembled into the audit trace."""

    code: str
    detail: str
    weight: float = 1.0


class Diagnosis(Frozen):
    diagnosis_id: str
    event_id: str
    failure_class: FailureClass
    confidence: float = Field(ge=0.0, le=1.0)
    downtime_overlap: DowntimeWindow | None = None
    segment_health: SegmentHealth = SegmentHealth.HEALTHY
    recommended_class: InterventionClass
    afa_required: bool = False
    evidence: list[EvidenceItem] = Field(default_factory=list)
    source: str = "table"  # "table" | "classifier" | "fallback"
    model_version: str = "unversioned"
    diagnosed_at: datetime | None = None


# ---------------------------------------------------------------------------
# L3 - decide
# ---------------------------------------------------------------------------


class Intervention(Frozen):
    intervention_id: str
    event_id: str
    channel: Channel
    message_class: MessageClass
    scheduled_for: datetime
    discount_paise: int = Field(default=0, ge=0)
    estimated_cost_paise: int = Field(default=0, ge=0)
    template_id: str | None = None
    requires_afa: bool = False

    @property
    def is_contact(self) -> bool:
        return self.channel in CONTACT_CHANNELS

    @model_validator(mode="after")
    def _promotional_is_never_a_recovery_action(self) -> Intervention:
        # TRAI-03 makes a mixed message promotional in its entirety. Antar never
        # schedules a promotional recovery contact, so a PROMOTIONAL candidate is a
        # construction bug rather than a policy decision.
        if self.message_class is MessageClass.PROMOTIONAL:
            raise ValueError(
                "recovery interventions may not be PROMOTIONAL (TRAI-03); "
                "if a draft turns promotional the contamination detector blocks it"
            )
        return self


class DecisionContext(Frozen):
    """The object every regulation predicate is evaluated against.

    Regulations are pure functions of this. That is what lets the constraint
    compiler turn them into LP rows and the property tests generate them at random.
    """

    event: AtRiskEvent
    customer: CustomerContext
    intervention: Intervention
    diagnosis: Diagnosis | None = None
    now: datetime
    afa_free_ceiling_paise: int
    contact_window_start_hour: int = 9
    contact_window_end_hour: int = 21
    lead_time_hours: int = 24
    contacts_allowed_per_30d: int = 3
    cooldown_hours: int = 72
    explicit_consent_validity_days: int = 7


class ConstraintViolation(Frozen):
    regulation_id: str
    severity: str
    explanation: str


class Decision(Frozen):
    decision_id: str
    event_id: str
    chosen: Intervention | None = None
    uplift_estimate: float = 0.0
    uplift_ci: tuple[float, float] = (0.0, 0.0)
    expected_incremental_paise: int = 0
    expected_cost_paise: int = 0
    binding_constraints: list[str] = Field(default_factory=list)
    stopping_rule_fired: str | None = None
    model_version: str = "unversioned"
    policy_version: str = "unversioned"
    is_control: bool = False
    arm: Arm = Arm.TREATMENT
    # Probability with which this action was selected. Required for IPS/DR-OPE.
    propensity: float = Field(default=1.0, gt=0.0, le=1.0)
    is_exploration: bool = False
    decided_at: datetime | None = None
    rationale: str = ""

    @property
    def acted(self) -> bool:
        return self.chosen is not None


# ---------------------------------------------------------------------------
# L4 - act
# ---------------------------------------------------------------------------


class DraftedMessage(Frozen):
    """What the LLM is allowed to produce: slot values for a registered template.

    It cannot emit a free-form body, cannot choose a channel, cannot set an amount,
    and cannot decide whether to send. N1.
    """

    template_id: str
    message_class: MessageClass
    slots: dict[str, str]
    rendered: str
    language: str = "en"
    prompt_version: str = "none"
    fallback_used: bool = False
    repair_attempts: int = 0
    contamination_score: float = 0.0


class ActionRecord(Frozen):
    action_id: str
    decision_id: str
    event_id: str
    idempotency_key: str
    channel: Channel
    message_class: MessageClass
    amount_paise: int = 0
    discount_paise: int = 0
    cost_paise: int = 0
    executed: bool = False
    dry_run: bool = True
    requires_human_approval: bool = False
    approved_by: str | None = None
    rejected_reason: str | None = None
    provider_reference: str | None = None
    draft: DraftedMessage | None = None
    attempted_at: datetime | None = None

    @property
    def blocked(self) -> bool:
        return self.rejected_reason is not None


class Outcome(Frozen):
    event_id: str
    recovered: bool
    recovered_paise: int = 0
    recovered_at: datetime | None = None
    optout: bool = False
    optout_at: datetime | None = None
    mandate_revoked: bool = False
    attempts: int = 0
    contacts: int = 0
    cost_paise: int = 0
    discount_paise: int = 0

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.cost_paise - self.discount_paise


# ---------------------------------------------------------------------------
# L5 - audit
# ---------------------------------------------------------------------------


class LedgerEntry(Frozen):
    seq: int
    prev_hash: str
    payload_hash: str
    kind: LedgerKind
    payload: dict[str, Any]
    written_at: datetime


__all__ = [
    "CONTACT_CHANNELS",
    "HIGH_CEILING_CATEGORIES",
    "TERMINAL_MANDATE_STATES",
    "ActionRecord",
    "Arm",
    "AtRiskEvent",
    "Channel",
    "ConsentBasis",
    "ConstraintViolation",
    "CustomerContext",
    "Decision",
    "DecisionContext",
    "Diagnosis",
    "DowntimeSeverity",
    "DowntimeStatus",
    "DowntimeWindow",
    "DraftedMessage",
    "EvidenceItem",
    "FailureClass",
    "Frozen",
    "Intervention",
    "InterventionClass",
    "LedgerEntry",
    "LedgerKind",
    "LossClass",
    "MandateState",
    "MerchantCategory",
    "MessageClass",
    "Outcome",
    "PaymentMethod",
    "SegmentHealth",
    "SegmentHealthReport",
]
