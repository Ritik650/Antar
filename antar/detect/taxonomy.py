"""Deterministic mapping from a Razorpay error payload to a failure cause.

**This module contains no LLM call, and neither does anything else in
`antar/detect`.** That claim is proven by
`tests/unit/test_no_llm_in_detection.py`, which makes any Anthropic call raise and
then runs the whole detection path.

## Why a table, and why the table is not enough

Most error reasons mean exactly one thing: `card_expired` is a technical decline and
nothing else. Those are a lookup, and a lookup is the right tool - it is auditable,
it has no training set, and it cannot drift.

But a real payment taxonomy is ambiguous in exactly the places that matter most:

  * `gateway_technical_error` is emitted when the issuer is down **and** when the
    instrument is broken. WAIT and TERMINATE are opposite decisions.
  * `declined_by_issuer` covers insufficient funds and risk declines, because banks
    routinely decline for funds without saying so.
  * `payment_failed` is a residual bucket and means nothing at all.

For those, the table returns `AMBIGUOUS` rather than guessing. Resolution is the
job of `root_cause.py`, which has evidence the error string does not: whether a
downtime window covers the attempt, whether the segment's success rate has broken
down, how much the debit was for, and what the mandate's state machine says.

**The `UNKNOWN` rate is reported, never hidden.** It is not a bug to have one. It is
a bug to pretend you do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from antar.signals.razorpay_errors import DOCUMENTED_REASONS
from antar.signals.schemas import AtRiskEvent, FailureClass

# Sentinel for "this code is genuinely ambiguous; ask someone with more evidence".
AMBIGUOUS = None


class Verdict(NamedTuple):
    failure_class: FailureClass
    confidence: float
    source: str
    rationale: str


@dataclass(frozen=True)
class TaxonomyRule:
    """One row of the lookup. `failure_class is None` means ambiguous."""

    failure_class: FailureClass | None
    confidence: float
    rationale: str
    candidates: tuple[FailureClass, ...] = ()
    """For ambiguous rows: the causes this code is consistent with. `root_cause.py`
    uses these to constrain its search rather than reconsidering all seven."""


# Written from the question "what does this error mean?", not by inverting the
# generator's emission table. The overlap below is what makes the rest of L2 do
# real work; see docs/SIMULATOR_CARD.md section 4.2.
TABLE: dict[str, TaxonomyRule] = {
    # ---- unambiguous: funds -------------------------------------------------
    "insufficient_funds": TaxonomyRule(
        FailureClass.INSUFFICIENT_FUNDS, 0.97, "The bank said so explicitly."
    ),
    "payment_limit_exceeded": TaxonomyRule(
        FailureClass.INSUFFICIENT_FUNDS,
        0.80,
        "A per-transaction or per-day limit. Funds-availability in substance: the "
        "money exists but cannot move today, and the remedy is the same - retry "
        "later, do not terminate.",
    ),
    "max_amount_exceeded": TaxonomyRule(
        FailureClass.INSUFFICIENT_FUNDS,
        0.65,
        "An instrument ceiling. Lower confidence because on an e-mandate this can "
        "equally be the AFA threshold being crossed.",
        candidates=(FailureClass.INSUFFICIENT_FUNDS, FailureClass.AFA_REQUIRED),
    ),
    # ---- unambiguous: issuer availability -----------------------------------
    "issuer_down": TaxonomyRule(
        FailureClass.ISSUER_DOWN, 0.95, "The gateway named the issuer as unavailable."
    ),
    # ---- unambiguous: instrument --------------------------------------------
    "card_expired": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.98, "An expired card does not fix itself."
    ),
    "card_blocked": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.92, "Blocked by the issuer; needs customer action."
    ),
    "invalid_vpa": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.95, "The UPI handle is wrong or has been closed."
    ),
    "account_does_not_exist": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.93, "The account behind the mandate is gone."
    ),
    "invalid_account": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.90, "Registered account details are wrong."
    ),
    "account_frozen": TaxonomyRule(
        FailureClass.TECHNICAL_DECLINE, 0.88, "Frozen by the bank; no debit will succeed."
    ),
    # ---- unambiguous: risk ---------------------------------------------------
    "payment_risk_check_failed": TaxonomyRule(
        FailureClass.RISK_DECLINE, 0.95, "A risk engine declined it."
    ),
    "risk_threshold_exceeded": TaxonomyRule(
        FailureClass.RISK_DECLINE, 0.92, "The issuer declined on risk grounds."
    ),
    # ---- unambiguous: mandate lifecycle -------------------------------------
    "mandate_revoked": TaxonomyRule(
        FailureClass.MANDATE_REVOKED,
        0.99,
        "Terminal. Never contact again about this mandate.",
    ),
    "subscription_cancelled": TaxonomyRule(
        FailureClass.MANDATE_REVOKED, 0.98, "Terminal."
    ),
    "invalid_mandate": TaxonomyRule(
        FailureClass.MANDATE_REVOKED,
        0.85,
        "The mandate will not authorise this debit. Usually revoked or expired; "
        "occasionally a registration defect that could be repaired.",
    ),
    # ---- unambiguous: AFA ----------------------------------------------------
    "additional_authentication_required": TaxonomyRule(
        FailureClass.AFA_REQUIRED,
        0.97,
        "RBI-EM-03/-04: above the category ceiling the customer must authenticate "
        "each time. Route to an AFA-bearing flow, which completes less often.",
    ),
    # ---- AMBIGUOUS: the interesting half ------------------------------------
    "gateway_technical_error": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "Emitted both when the issuer is unavailable and when the instrument is "
        "broken. WAIT and TERMINATE are opposite decisions, so guessing here is "
        "expensive in both directions. Resolved by the downtime cross-check.",
        candidates=(FailureClass.ISSUER_DOWN, FailureClass.TECHNICAL_DECLINE),
    ),
    "server_error": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "The bank's server erred. Usually an outage, sometimes an instrument the "
        "bank cannot process.",
        candidates=(FailureClass.ISSUER_DOWN, FailureClass.TECHNICAL_DECLINE),
    ),
    "payment_timed_out": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "A timeout says something was slow, not who was at fault.",
        candidates=(FailureClass.ISSUER_DOWN, FailureClass.TECHNICAL_DECLINE),
    ),
    "network_error": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "Same as a timeout: no attribution.",
        candidates=(FailureClass.ISSUER_DOWN, FailureClass.TECHNICAL_DECLINE),
    ),
    "declined_by_issuer": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "The generic issuer decline. Banks routinely decline for insufficient funds "
        "without saying so, and also decline on risk. Separating them decides "
        "between rescheduling toward payday and stopping entirely.",
        candidates=(FailureClass.INSUFFICIENT_FUNDS, FailureClass.RISK_DECLINE),
    ),
    "incorrect_otp": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "The customer failed an authentication step. Whether that step existed "
        "because of the AFA threshold or because the instrument demanded it depends "
        "on the amount and the category.",
        candidates=(FailureClass.AFA_REQUIRED, FailureClass.TECHNICAL_DECLINE),
    ),
    "authentication_failed": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "As `incorrect_otp`.",
        candidates=(FailureClass.AFA_REQUIRED, FailureClass.TECHNICAL_DECLINE),
    ),
    "payment_cancelled": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "The customer abandoned this payment. That is not the same as revoking the "
        "mandate, and treating it as terminal would throw away a recoverable cycle.",
        candidates=(FailureClass.MANDATE_REVOKED, FailureClass.TECHNICAL_DECLINE),
    ),
    "payment_failed": TaxonomyRule(
        AMBIGUOUS,
        0.0,
        "The residual bucket. Carries no information whatsoever.",
        candidates=(),
    ),
}


AMBIGUOUS_REASONS: frozenset[str] = frozenset(
    reason for reason, rule in TABLE.items() if rule.failure_class is AMBIGUOUS
)
RESOLVED_REASONS: frozenset[str] = frozenset(TABLE) - AMBIGUOUS_REASONS


def classify(event: AtRiskEvent) -> Verdict:
    """Table lookup only. Returns UNKNOWN for anything the table cannot resolve.

    Deliberately takes no downtime registry, no segment health, and no classifier:
    this function is the auditable floor of the detection layer, and keeping it a
    pure function of the payload is what makes it auditable.
    """
    reason = (event.error_reason or "").strip()

    if not reason:
        return Verdict(
            FailureClass.UNKNOWN,
            0.0,
            "table",
            "No error_reason on the payload.",
        )

    rule = TABLE.get(reason)
    if rule is None:
        return Verdict(
            FailureClass.UNKNOWN,
            0.0,
            "table",
            f"Reason {reason!r} is not in the taxonomy table."
            + (
                " It is also not in the documented Razorpay taxonomy, which means "
                "either the catalogue is stale or the payload is not what we think."
                if reason not in DOCUMENTED_REASONS
                else " It is a documented reason with no rule, which is a gap to fill."
            ),
        )

    if rule.failure_class is AMBIGUOUS:
        return Verdict(FailureClass.UNKNOWN, 0.0, "table", rule.rationale)

    return Verdict(rule.failure_class, rule.confidence, "table", rule.rationale)


def candidates_for(event: AtRiskEvent) -> tuple[FailureClass, ...]:
    """Causes an ambiguous code is consistent with. Empty means no constraint."""
    rule = TABLE.get((event.error_reason or "").strip())
    if rule is None:
        return ()
    if rule.failure_class is AMBIGUOUS:
        return rule.candidates
    return (rule.failure_class, *rule.candidates)


def is_ambiguous(event: AtRiskEvent) -> bool:
    return (event.error_reason or "").strip() in AMBIGUOUS_REASONS


def coverage_report() -> dict[str, object]:
    """What the table does and does not cover. Surfaced in the console."""
    missing = sorted(DOCUMENTED_REASONS - set(TABLE))
    extra = sorted(set(TABLE) - DOCUMENTED_REASONS)
    return {
        "documented_reasons": len(DOCUMENTED_REASONS),
        "rules": len(TABLE),
        "resolved_by_table": len(RESOLVED_REASONS),
        "ambiguous": sorted(AMBIGUOUS_REASONS),
        "documented_but_unmapped": missing,
        "mapped_but_undocumented": extra,
    }
