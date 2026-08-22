"""Fusing the table, the downtime feed, the changepoint detector, the mandate FSM,
and the classifier into one `Diagnosis`.

**No LLM anywhere in this package.** `tests/unit/test_no_llm_in_detection.py` makes
every Anthropic call raise and runs this whole path.

## The four separations that pay for this layer

PLAN.md M3 names them, and they are the reason L2 is not just a lookup:

| Cause | What to do | Why the distinction is worth money |
|---|---|---|
| `ISSUER_DOWN` | **WAIT** | They could not have paid. An attempt costs a notification, and under RBI-EM-02 that notification carries an opt-out. Retrying into an outage manufactures cancellations. |
| `INSUFFICIENT_FUNDS` | **RESCHEDULE** | The money arrives on payday. Asking on the 28th and asking on the 2nd are different questions. |
| `MANDATE_REVOKED` | **TERMINATE** | Contacting someone who cancelled is a support ticket and, if the message class is wrong, a TRAI violation. |
| `AFA_REQUIRED` | **CONTACT**, AFA-bearing flow | Above the RBI-EM-03/-04 ceiling the customer must authenticate every time, and that flow completes materially less often. Route it, and price it accordingly. |

## Evidence order

Deterministic evidence first, model evidence last:

1. **Mandate state.** A paused or revoked mandate settles the question before any
   error code is read. RBI-EM-06 makes pausing a customer right, not a fault.
2. **The taxonomy table.** Unambiguous codes are believed. No model overrules
   `card_expired`.
3. **The downtime cross-check.** For ambiguous codes, a covering outage window is
   strong, auditable, third-party evidence.
4. **Segment changepoint.** No declared outage, but the segment has broken down.
5. **The AFA boundary.** Above the ceiling with an authentication-flavoured code.
6. **The classifier**, restricted to the causes the table says are possible.
7. **`UNKNOWN` → WAIT.** The safe failure for a money system is doing nothing.

Every step appends an `EvidenceItem`, so the console can answer "why did you conclude
that?" with the actual chain rather than a score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from antar import clock
from antar.detect.classifier import DetectionContext, FailureClassifier, afa_ceiling_for
from antar.detect.mandate_fsm import MandateRegistry
from antar.detect.taxonomy import candidates_for, classify, is_ambiguous
from antar.ids import diagnosis_id
from antar.signals.downtime import SEVERITY_WEIGHT, DowntimeRegistry
from antar.signals.schemas import (
    AtRiskEvent,
    Diagnosis,
    EvidenceItem,
    FailureClass,
    InterventionClass,
    MandateState,
    SegmentHealth,
)

# What L2 recommends L3 consider. L3 is free to disagree - it has the uplift estimate
# and the constraint set, which L2 does not - but it must record that it did.
RECOMMENDATION: dict[FailureClass, InterventionClass] = {
    FailureClass.ISSUER_DOWN: InterventionClass.WAIT,
    FailureClass.INSUFFICIENT_FUNDS: InterventionClass.RESCHEDULE,
    FailureClass.MANDATE_REVOKED: InterventionClass.TERMINATE,
    FailureClass.AFA_REQUIRED: InterventionClass.CONTACT,
    FailureClass.TECHNICAL_DECLINE: InterventionClass.CONTACT,
    # A risk decline will usually decline again, and pushing at it looks like
    # exactly the behaviour the risk engine is there to stop.
    FailureClass.RISK_DECLINE: InterventionClass.WAIT,
    FailureClass.UNKNOWN: InterventionClass.WAIT,
}

# Confidence assigned when a non-table rule fires. Lower than the table's, because
# these are inferences rather than readings.
DOWNTIME_CONFIDENCE = 0.90
CHANGEPOINT_CONFIDENCE = 0.62
AFA_BOUNDARY_CONFIDENCE = 0.70
MANDATE_STATE_CONFIDENCE = 0.99


@dataclass
class RootCauseAnalyser:
    """Assembles a `Diagnosis` from every source of evidence available."""

    downtime: DowntimeRegistry
    mandates: MandateRegistry
    classifier: FailureClassifier | None = None
    changepoint: object | None = None  # ChangepointDetector; kept loose to avoid a cycle
    downtime_tolerance_minutes: int = 30
    afa_ceilings: dict[str, int] | None = None
    model_version: str = "detect-v1"

    def diagnose(self, event: AtRiskEvent, *, prior_attempts: int = 0) -> Diagnosis:
        evidence: list[EvidenceItem] = []
        at = event.occurred_at

        segment_health = self._segment_health(event, at)
        overlap = self.downtime.overlap_for(
            at, event.method, event.issuer, tolerance_minutes=self.downtime_tolerance_minutes
        )
        ceiling = afa_ceiling_for(event, self.afa_ceilings)
        above_ceiling = event.amount_paise > ceiling

        # --- 1. mandate state settles it before anything else ------------------
        # `state_at`, not `state_of`: the state as of the failure, using only what
        # was knowable then. See POSTMORTEM D10.
        state = self.mandates.state_at(event.subscription_id, at)
        if state in (MandateState.REVOKED, MandateState.COMPLETED):
            evidence.append(
                EvidenceItem(
                    code="MANDATE_STATE",
                    detail=f"Mandate is {state.value}; no debit against it can succeed.",
                    weight=1.0,
                )
            )
            return self._build(
                event,
                FailureClass.MANDATE_REVOKED,
                MANDATE_STATE_CONFIDENCE,
                "mandate_fsm",
                evidence,
                overlap,
                segment_health,
                above_ceiling,
                override=InterventionClass.TERMINATE,
            )

        if state is MandateState.PAUSED:
            evidence.append(
                EvidenceItem(
                    code="MANDATE_PAUSED",
                    detail=(
                        "The customer paused this mandate (RBI-EM-06). A paused "
                        "mandate is not a retry candidate."
                    ),
                    weight=1.0,
                )
            )
            return self._build(
                event,
                FailureClass.UNKNOWN,
                MANDATE_STATE_CONFIDENCE,
                "mandate_fsm",
                evidence,
                overlap,
                segment_health,
                above_ceiling,
                override=InterventionClass.WAIT,
            )

        # --- 2. the taxonomy table ---------------------------------------------
        verdict = classify(event)
        if verdict.failure_class is not FailureClass.UNKNOWN:
            evidence.append(
                EvidenceItem(
                    code="TAXONOMY",
                    detail=f"{event.error_reason!r}: {verdict.rationale}",
                    weight=verdict.confidence,
                )
            )
            # One correction the table cannot make on its own: an unambiguous
            # customer-side decline that happens to land inside a declared outage is
            # far more likely to be the outage.
            if overlap is not None and verdict.failure_class in (
                FailureClass.TECHNICAL_DECLINE,
                FailureClass.RISK_DECLINE,
            ):
                evidence.append(
                    EvidenceItem(
                        code="DOWNTIME_OVERRIDE",
                        detail=(
                            f"A {overlap.severity.value}-severity outage on "
                            f"{event.segment_key} covers this attempt, so the decline "
                            "is more likely the rail than the instrument."
                        ),
                        weight=SEVERITY_WEIGHT[overlap.severity],
                    )
                )
                if SEVERITY_WEIGHT[overlap.severity] >= 0.70:
                    return self._build(
                        event, FailureClass.ISSUER_DOWN, DOWNTIME_CONFIDENCE, "downtime",
                        evidence, overlap, segment_health, above_ceiling,
                    )
            return self._build(
                event, verdict.failure_class, verdict.confidence, "table",
                evidence, overlap, segment_health, above_ceiling,
            )

        # --- 3-6. ambiguous: bring the other evidence to bear -------------------
        if is_ambiguous(event):
            evidence.append(
                EvidenceItem(
                    code="AMBIGUOUS_CODE",
                    detail=(
                        f"{event.error_reason!r} is consistent with "
                        f"{[c.value for c in candidates_for(event)] or 'any cause'}; "
                        "the table cannot resolve it."
                    ),
                    weight=0.0,
                )
            )

        allowed = set(candidates_for(event))

        if overlap is not None and (not allowed or FailureClass.ISSUER_DOWN in allowed):
            evidence.append(
                EvidenceItem(
                    code="DOWNTIME_OVERLAP",
                    detail=(
                        f"Downtime {overlap.downtime_id} ({overlap.severity.value}) on "
                        f"{event.segment_key} covers {at.isoformat()}."
                    ),
                    weight=SEVERITY_WEIGHT[overlap.severity],
                )
            )
            return self._build(
                event, FailureClass.ISSUER_DOWN, DOWNTIME_CONFIDENCE, "downtime",
                evidence, overlap, segment_health, above_ceiling,
            )

        # DEGRADED only, not DEGRADING. Calling an outage on the weaker signal costs
        # precision on ISSUER_DOWN, and a false ISSUER_DOWN means waiting on a cycle
        # that was recoverable - the most expensive misclassification in the matrix.
        if segment_health is SegmentHealth.DEGRADED and (
            not allowed or FailureClass.ISSUER_DOWN in allowed
        ):
            evidence.append(
                EvidenceItem(
                    code="SEGMENT_CHANGEPOINT",
                    detail=(
                        f"No declared outage, but {event.segment_key} is "
                        f"{segment_health.value} on the CUSUM. An undeclared outage "
                        "is the most likely explanation."
                    ),
                    weight=0.6,
                )
            )
            return self._build(
                event, FailureClass.ISSUER_DOWN, CHANGEPOINT_CONFIDENCE, "changepoint",
                evidence, overlap, segment_health, above_ceiling,
            )

        if above_ceiling and FailureClass.AFA_REQUIRED in allowed:
            evidence.append(
                EvidenceItem(
                    code="AFA_BOUNDARY",
                    detail=(
                        f"Rs {event.amount_paise / 100:,.0f} is above the "
                        f"Rs {ceiling / 100:,.0f} AFA-free ceiling for "
                        f"{event.merchant_category.value} (RBI-EM-03/-04), and the "
                        "error is authentication-flavoured."
                    ),
                    weight=0.7,
                )
            )
            return self._build(
                event, FailureClass.AFA_REQUIRED, AFA_BOUNDARY_CONFIDENCE, "afa_boundary",
                evidence, overlap, segment_health, above_ceiling,
            )

        if self.classifier is not None and self.classifier.fitted:
            context = DetectionContext(
                segment_health=segment_health, downtime=overlap, prior_attempts=prior_attempts
            )
            predicted, confidence = self.classifier.predict_one(
                event, context, ceilings=self.afa_ceilings
            )
            evidence.append(
                EvidenceItem(
                    code="CLASSIFIER",
                    detail=(
                        f"{self.classifier.version} predicts {predicted.value} at "
                        f"p={confidence:.2f}"
                        + (
                            f" (abstained; below {self.classifier.abstain_below})"
                            if predicted is FailureClass.UNKNOWN
                            else ""
                        )
                    ),
                    weight=confidence,
                )
            )
            if predicted is not FailureClass.UNKNOWN:
                return self._build(
                    event, predicted, confidence, "classifier",
                    evidence, overlap, segment_health, above_ceiling,
                )

        # --- 7. honest UNKNOWN --------------------------------------------------
        evidence.append(
            EvidenceItem(
                code="UNRESOLVED",
                detail=(
                    "No evidence resolves this failure. Recommending WAIT: the safe "
                    "failure for a money system is doing nothing."
                ),
                weight=0.0,
            )
        )
        return self._build(
            event, FailureClass.UNKNOWN, 0.0, "fallback",
            evidence, overlap, segment_health, above_ceiling,
        )

    # ------------------------------------------------------------------ helpers

    def _segment_health(self, event: AtRiskEvent, at: datetime) -> SegmentHealth:
        if self.changepoint is None:
            return SegmentHealth.HEALTHY
        return self.changepoint.health_at(event.segment_key, at)

    def _build(
        self,
        event: AtRiskEvent,
        failure_class: FailureClass,
        confidence: float,
        source: str,
        evidence: list[EvidenceItem],
        overlap,
        segment_health: SegmentHealth,
        above_ceiling: bool,
        *,
        override: InterventionClass | None = None,
    ) -> Diagnosis:
        recommended = override or RECOMMENDATION[failure_class]
        return Diagnosis(
            diagnosis_id=diagnosis_id(event.event_id, self.model_version),
            event_id=event.event_id,
            failure_class=failure_class,
            confidence=round(float(confidence), 4),
            downtime_overlap=overlap,
            segment_health=segment_health,
            recommended_class=recommended,
            afa_required=above_ceiling or failure_class is FailureClass.AFA_REQUIRED,
            evidence=evidence,
            source=source,
            model_version=self.model_version,
            diagnosed_at=clock.now(),
        )

    def diagnose_all(self, events, *, prior_attempts: dict[str, int] | None = None):
        counts = prior_attempts or {}
        return [
            self.diagnose(event, prior_attempts=counts.get(event.customer_id, 0))
            for event in events
        ]


# ---------------------------------------------------------------------------
# The cost of being wrong, denominated in rupees and indexed by **action**.
#
# An earlier version indexed this by predicted *label*, which produced a
# nonsensical ablation: converting an `UNKNOWN` into a wrong `ISSUER_DOWN` appeared
# to cost more, even though both recommend WAIT and the merchant does exactly the
# same thing in each case. A detector is not graded on the name it assigns; it is
# graded on what the name causes to happen. POSTMORTEM D11.
#
# Read as: cost of taking `action` when the truth was `cause`, as a multiple of the
# cycle amount. Every value is author-chosen and the reasoning is in the comment.
# ---------------------------------------------------------------------------
ACTION_COST: dict[tuple[InterventionClass, FailureClass], float] = {
    # WAIT ----------------------------------------------------------------
    (InterventionClass.WAIT, FailureClass.ISSUER_DOWN): 0.00,  # correct
    (InterventionClass.WAIT, FailureClass.RISK_DECLINE): 0.00,  # correct
    (InterventionClass.WAIT, FailureClass.MANDATE_REVOKED): 0.00,  # nothing to lose
    # The account refills on payday whether or not we asked, so waiting costs the
    # increment we would have won by timing it, not the cycle.
    (InterventionClass.WAIT, FailureClass.INSUFFICIENT_FUNDS): 0.15,
    # An expired card does not fix itself. Waiting loses the cycle.
    (InterventionClass.WAIT, FailureClass.TECHNICAL_DECLINE): 0.60,
    (InterventionClass.WAIT, FailureClass.AFA_REQUIRED): 0.50,
    # RESCHEDULE ----------------------------------------------------------
    (InterventionClass.RESCHEDULE, FailureClass.INSUFFICIENT_FUNDS): 0.00,  # correct
    # A delayed retry into an outage is close to harmless - by the time it fires the
    # outage has usually passed - but it still spends a notification.
    (InterventionClass.RESCHEDULE, FailureClass.ISSUER_DOWN): 0.08,
    (InterventionClass.RESCHEDULE, FailureClass.TECHNICAL_DECLINE): 0.40,
    (InterventionClass.RESCHEDULE, FailureClass.AFA_REQUIRED): 0.40,
    (InterventionClass.RESCHEDULE, FailureClass.RISK_DECLINE): 0.20,
    (InterventionClass.RESCHEDULE, FailureClass.MANDATE_REVOKED): 0.25,
    # CONTACT -------------------------------------------------------------
    (InterventionClass.CONTACT, FailureClass.TECHNICAL_DECLINE): 0.00,  # correct
    (InterventionClass.CONTACT, FailureClass.AFA_REQUIRED): 0.00,  # correct
    (InterventionClass.CONTACT, FailureClass.INSUFFICIENT_FUNDS): 0.10,
    # Contacting during an outage: a wasted message, a wasted contact slot, and an
    # RBI-EM-02 opt-out prompt delivered to someone who did nothing wrong.
    (InterventionClass.CONTACT, FailureClass.ISSUER_DOWN): 0.30,
    (InterventionClass.CONTACT, FailureClass.RISK_DECLINE): 0.20,
    # Messaging someone who has already cancelled: a support ticket at best, and a
    # TRAI exposure if the message class is wrong.
    (InterventionClass.CONTACT, FailureClass.MANDATE_REVOKED): 0.35,
    # TERMINATE -----------------------------------------------------------
    (InterventionClass.TERMINATE, FailureClass.MANDATE_REVOKED): 0.00,  # correct
    # Giving up on a customer who would have paid. The most expensive error there is,
    # and the one nobody notices, because the counterfactual never shows up.
    (InterventionClass.TERMINATE, FailureClass.ISSUER_DOWN): 1.00,
    (InterventionClass.TERMINATE, FailureClass.INSUFFICIENT_FUNDS): 1.00,
    (InterventionClass.TERMINATE, FailureClass.TECHNICAL_DECLINE): 0.80,
    (InterventionClass.TERMINATE, FailureClass.AFA_REQUIRED): 0.80,
    (InterventionClass.TERMINATE, FailureClass.RISK_DECLINE): 0.50,
}


def action_cost(action: InterventionClass, actual: FailureClass) -> float:
    """Cost of `action` given the true cause, as a multiple of the cycle amount.

    An unknown true cause returns 0: we cannot charge the detector for an error we
    cannot demonstrate.
    """
    if actual is FailureClass.UNKNOWN:
        return 0.0
    return ACTION_COST.get((action, actual), 0.25)


def diagnosis_cost_paise(diagnosis: Diagnosis, actual: FailureClass, amount_paise: int) -> int:
    return round(amount_paise * action_cost(diagnosis.recommended_class, actual))


def unknown_rate(diagnoses) -> float:
    """The number PLAN.md M3 insists is reported rather than hidden."""
    total = len(diagnoses)
    if not total:
        return 0.0
    return sum(1 for d in diagnoses if d.failure_class is FailureClass.UNKNOWN) / total


def source_mix(diagnoses) -> dict[str, int]:
    """Which evidence actually decided each case.

    Worth reporting on its own: if `table` decided 95% of events, the classifier and
    the downtime cross-check are not earning their place and the architecture should
    say so.
    """
    from collections import Counter

    return dict(sorted(Counter(d.source for d in diagnoses).items()))
