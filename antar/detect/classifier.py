"""LightGBM over the cases the taxonomy table cannot resolve.

**No LLM.** A gradient-boosted tree over twelve features is the right tool here and a
language model is not: this is a tabular multiclass problem with a training set, a
measurable calibration, and a cost matrix denominated in rupees. See
`docs/ARCHITECTURE.md`.

## What it is asked to do, and what it is not

It runs on the ~25% of events whose error code is genuinely ambiguous
(`gateway_technical_error`, `declined_by_issuer`, `payment_failed`, ...). It is
**constrained by the table**: the taxonomy says which causes a given code is
consistent with, and the classifier may only choose among those. A model that decided
`card_expired` meant `ISSUER_DOWN` would be overruled by the table before anyone saw
it.

It **abstains**. Below `abstain_below` on the top class it returns `UNKNOWN` rather
than a low-confidence guess, and the abstention rate is reported. For a system whose
safe failure mode is doing nothing, an honest `UNKNOWN` is worth more than a coin
flip: `UNKNOWN` routes to `WAIT`, which costs a delay, while a wrong confident
`ISSUER_DOWN` costs a whole recoverable cycle.

## Where the labels come from

In this build, from the simulator's ground truth - restricted to the treatment arm,
so that no control-group event is ever used for fitting anything
(`docs/EVALUATION.md` section 3.4). In production the labels would come from eventual
outcome plus manual review of a sample, which is slower, noisier, and the honest
answer to "how would you actually train this". `docs/LIMITATIONS.md` L8.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from antar.detect.taxonomy import candidates_for
from antar.signals.schemas import (
    HIGH_CEILING_CATEGORIES,
    AtRiskEvent,
    DowntimeWindow,
    FailureClass,
    SegmentHealth,
)

# Order is fixed and explicit so that a model trained in one process scores the same
# in another. Relying on set iteration order here would be a determinism bug.
CLASS_ORDER: tuple[FailureClass, ...] = (
    FailureClass.INSUFFICIENT_FUNDS,
    FailureClass.ISSUER_DOWN,
    FailureClass.MANDATE_REVOKED,
    FailureClass.AFA_REQUIRED,
    FailureClass.TECHNICAL_DECLINE,
    FailureClass.RISK_DECLINE,
)
CLASS_INDEX: dict[FailureClass, int] = {cls: i for i, cls in enumerate(CLASS_ORDER)}

CATEGORICAL = [
    "error_reason",
    "error_source",
    "error_step",
    "error_code",
    "method",
    "issuer",
    "merchant_category",
    "segment_health",
    "downtime_severity",
]

NUMERIC = [
    "log_amount",
    "above_afa_ceiling",
    "cycle_number",
    "hour_of_day",
    "day_of_month",
    "prior_attempts",
    "downtime_overlap",
]

FEATURES = CATEGORICAL + NUMERIC

SEGMENT_HEALTH_ORDER = {
    SegmentHealth.HEALTHY: "HEALTHY",
    SegmentHealth.RECOVERING: "RECOVERING",
    SegmentHealth.DEGRADING: "DEGRADING",
    SegmentHealth.DEGRADED: "DEGRADED",
}


@dataclass(frozen=True)
class DetectionContext:
    """The evidence available to the classifier beyond the payload itself.

    Explicitly *not* a latent. Every field here is something a real merchant could
    compute from their own data plus Razorpay's Downtime API.
    """

    segment_health: SegmentHealth = SegmentHealth.HEALTHY
    downtime: DowntimeWindow | None = None
    prior_attempts: int = 0


def afa_ceiling_for(event: AtRiskEvent, ceilings: dict[str, int] | None = None) -> int:
    if ceilings:
        return int(ceilings.get(event.merchant_category.value, ceilings["DEFAULT"]))
    return 10_000_000 if event.merchant_category in HIGH_CEILING_CATEGORIES else 1_500_000


def build_row(
    event: AtRiskEvent,
    context: DetectionContext | None = None,
    *,
    ceilings: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One feature row. Pure function of observable data - see test_no_leakage."""
    ctx = context or DetectionContext()
    ceiling = afa_ceiling_for(event, ceilings)
    return {
        "error_reason": event.error_reason or "MISSING",
        "error_source": event.error_source or "MISSING",
        "error_step": event.error_step or "MISSING",
        "error_code": event.error_code or "MISSING",
        "method": event.method.value,
        "issuer": event.issuer,
        "merchant_category": event.merchant_category.value,
        "segment_health": SEGMENT_HEALTH_ORDER[ctx.segment_health],
        "downtime_severity": ctx.downtime.severity.value if ctx.downtime else "NONE",
        "log_amount": float(np.log1p(event.amount_paise)),
        "above_afa_ceiling": float(event.amount_paise > ceiling),
        "cycle_number": float(event.cycle_number or 0),
        "hour_of_day": float(event.occurred_at.hour),
        "day_of_month": float(event.occurred_at.day),
        "prior_attempts": float(ctx.prior_attempts),
        "downtime_overlap": float(ctx.downtime is not None),
    }


def build_frame(
    events: Sequence[AtRiskEvent],
    contexts: Sequence[DetectionContext] | None = None,
    *,
    ceilings: dict[str, int] | None = None,
) -> pd.DataFrame:
    contexts = contexts or [DetectionContext()] * len(events)
    frame = pd.DataFrame(
        [build_row(e, c, ceilings=ceilings) for e, c in zip(events, contexts, strict=True)]
    )
    for column in CATEGORICAL:
        frame[column] = frame[column].astype("category")
    return frame[FEATURES]


@dataclass
class ClassifierReport:
    """Per-class precision and recall, plus the abstention rate.

    PLAN.md M3 asks for the false-positive cost **in rupees**, not just a confusion
    matrix, because the classes are not equally expensive to get wrong: calling a
    revoked mandate `INSUFFICIENT_FUNDS` spends attempts on a dead subscription, and
    calling an outage `TECHNICAL_DECLINE` terminates a customer who was fine.
    """

    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    abstention_rate: float = 0.0
    accuracy_when_confident: float = 0.0
    support: int = 0
    false_positive_cost_paise: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "support": self.support,
            "abstention_rate": round(self.abstention_rate, 4),
            "accuracy_when_confident": round(self.accuracy_when_confident, 4),
            "per_class": self.per_class,
            "false_positive_cost_paise": self.false_positive_cost_paise,
        }


class FailureClassifier:
    """Multiclass LightGBM, constrained by the taxonomy and allowed to abstain."""

    def __init__(
        self,
        *,
        n_estimators: int = 200,
        learning_rate: float = 0.08,
        num_leaves: int = 31,
        min_child_samples: int = 40,
        abstain_below: float = 0.45,
        seed: int = 20260822,
    ) -> None:
        self.params = {
            "n_estimators": n_estimators,
            "learning_rate": learning_rate,
            "num_leaves": num_leaves,
            "min_child_samples": min_child_samples,
        }
        self.abstain_below = abstain_below
        self.seed = seed
        self.model: Any = None
        self.categories: dict[str, list[str]] = {}
        self.version = "unfitted"

    @classmethod
    def from_config(cls, config, *, seed: int | None = None) -> FailureClassifier:
        section = config.section("detect.classifier")
        return cls(
            n_estimators=int(section.get("n_estimators")),
            learning_rate=float(section.get("learning_rate")),
            num_leaves=int(section.get("num_leaves")),
            min_child_samples=int(section.get("min_child_samples")),
            abstain_below=float(section.get("abstain_below")),
            seed=seed if seed is not None else int(config.get("run.seed")),
        )

    # ------------------------------------------------------------------- fit

    def fit(
        self,
        events: Sequence[AtRiskEvent],
        labels: Sequence[FailureClass],
        contexts: Sequence[DetectionContext] | None = None,
        *,
        ceilings: dict[str, int] | None = None,
    ) -> FailureClassifier:
        import hashlib

        import lightgbm as lgb

        frame = build_frame(events, contexts, ceilings=ceilings)
        # Freeze the category levels so that scoring a single row later produces the
        # same encoding as training did. Without this, a batch of one gets a
        # different category mapping and the predictions are silently garbage.
        self.categories = {c: sorted(frame[c].cat.categories.tolist()) for c in CATEGORICAL}
        for column in CATEGORICAL:
            frame[column] = frame[column].cat.set_categories(self.categories[column])

        y = np.array([CLASS_INDEX[label] for label in labels])
        self.model = lgb.LGBMClassifier(
            objective="multiclass",
            num_class=len(CLASS_ORDER),
            random_state=self.seed,
            verbose=-1,
            deterministic=True,
            force_row_wise=True,
            **self.params,
        )
        self.model.fit(frame, y, categorical_feature=CATEGORICAL)

        digest = hashlib.blake2b(
            f"{self.params}|{self.seed}|{len(events)}".encode(), digest_size=6
        ).hexdigest()
        self.version = f"lgbm-{digest}"
        return self

    @property
    def fitted(self) -> bool:
        return self.model is not None

    # --------------------------------------------------------------- predict

    def _frame_for(
        self,
        events: Sequence[AtRiskEvent],
        contexts: Sequence[DetectionContext] | None,
        ceilings: dict[str, int] | None,
    ) -> pd.DataFrame:
        frame = build_frame(events, contexts, ceilings=ceilings)
        for column in CATEGORICAL:
            frame[column] = frame[column].cat.set_categories(self.categories[column])
        return frame

    def predict_proba(
        self,
        events: Sequence[AtRiskEvent],
        contexts: Sequence[DetectionContext] | None = None,
        *,
        ceilings: dict[str, int] | None = None,
    ) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError(
                "classifier is not fitted; refusing to guess. A money system that "
                "silently falls back to a default class is worse than one that stops."
            )
        return np.asarray(self.model.predict_proba(self._frame_for(events, contexts, ceilings)))

    def predict_one(
        self,
        event: AtRiskEvent,
        context: DetectionContext | None = None,
        *,
        ceilings: dict[str, int] | None = None,
        restrict_to_candidates: bool = True,
    ) -> tuple[FailureClass, float]:
        """The single-event path used by `root_cause.py`.

        `restrict_to_candidates` is what keeps the model inside the table's ruling:
        an ambiguous code is consistent with a named set of causes, and the model
        chooses among those rather than reopening the question.
        """
        proba = self.predict_proba([event], [context] if context else None, ceilings=ceilings)[0]

        allowed = candidates_for(event) if restrict_to_candidates else CLASS_ORDER
        allowed = tuple(c for c in (allowed or CLASS_ORDER) if c in CLASS_INDEX)
        if not allowed:
            allowed = CLASS_ORDER

        masked = np.zeros_like(proba)
        for cls in allowed:
            masked[CLASS_INDEX[cls]] = proba[CLASS_INDEX[cls]]
        total = masked.sum()
        if total <= 0:
            return FailureClass.UNKNOWN, 0.0
        masked = masked / total

        best = int(np.argmax(masked))
        confidence = float(masked[best])
        if confidence < self.abstain_below:
            return FailureClass.UNKNOWN, confidence
        return CLASS_ORDER[best], confidence

    # -------------------------------------------------------------- evaluate

    def evaluate(
        self,
        events: Sequence[AtRiskEvent],
        labels: Sequence[FailureClass],
        contexts: Sequence[DetectionContext] | None = None,
        *,
        ceilings: dict[str, int] | None = None,
    ) -> ClassifierReport:
        contexts = contexts or [DetectionContext()] * len(events)
        predictions: list[FailureClass] = []
        for event, context in zip(events, contexts, strict=True):
            predicted, _ = self.predict_one(event, context, ceilings=ceilings)
            predictions.append(predicted)

        report = ClassifierReport(support=len(events))
        abstained = sum(1 for p in predictions if p is FailureClass.UNKNOWN)
        report.abstention_rate = abstained / max(len(events), 1)

        confident = [
            (p, t) for p, t in zip(predictions, labels, strict=True) if p is not FailureClass.UNKNOWN
        ]
        report.accuracy_when_confident = (
            sum(1 for p, t in confident if p == t) / len(confident) if confident else 0.0
        )

        for cls in CLASS_ORDER:
            tp = sum(1 for p, t in zip(predictions, labels, strict=True) if p == cls and t == cls)
            fp = sum(1 for p, t in zip(predictions, labels, strict=True) if p == cls and t != cls)
            fn = sum(1 for p, t in zip(predictions, labels, strict=True) if p != cls and t == cls)
            report.per_class[cls.value] = {
                "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
                "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
                "support": tp + fn,
                "predicted": tp + fp,
            }
            report.false_positive_cost_paise[cls.value] = sum(
                event.amount_paise * misclassification_cost(cls, true)
                for event, p, true in zip(events, predictions, labels, strict=True)
                if p == cls and true != cls
            )

        return report


# How expensive it is to predict `predicted` when the truth is `actual`, as a
# multiple of the cycle amount. Author-chosen, and the reasoning is in the comments
# because a cost matrix without a rationale is just more numbers.
MISCLASSIFICATION_COST: dict[tuple[FailureClass, FailureClass], float] = {
    # Calling an outage a terminal instrument problem abandons a customer who would
    # have paid on their own. Full loss of the cycle.
    (FailureClass.TECHNICAL_DECLINE, FailureClass.ISSUER_DOWN): 1.0,
    (FailureClass.MANDATE_REVOKED, FailureClass.ISSUER_DOWN): 1.0,
    # Calling a revoked mandate anything else spends attempts and notifications on a
    # subscription that no longer exists - wasted contact budget, and a notification
    # to someone who already cancelled is a support ticket.
    (FailureClass.INSUFFICIENT_FUNDS, FailureClass.MANDATE_REVOKED): 0.30,
    (FailureClass.TECHNICAL_DECLINE, FailureClass.MANDATE_REVOKED): 0.30,
    # Treating a customer-side decline as an outage means waiting instead of acting,
    # which usually costs a delay rather than the cycle.
    (FailureClass.ISSUER_DOWN, FailureClass.INSUFFICIENT_FUNDS): 0.25,
    (FailureClass.ISSUER_DOWN, FailureClass.TECHNICAL_DECLINE): 0.40,
    # Missing an AFA requirement routes to a flow that cannot complete.
    (FailureClass.INSUFFICIENT_FUNDS, FailureClass.AFA_REQUIRED): 0.50,
    (FailureClass.TECHNICAL_DECLINE, FailureClass.AFA_REQUIRED): 0.50,
}
DEFAULT_MISCLASSIFICATION_COST = 0.15


def misclassification_cost(predicted: FailureClass, actual: FailureClass) -> float:
    if predicted == actual:
        return 0.0
    return MISCLASSIFICATION_COST.get((predicted, actual), DEFAULT_MISCLASSIFICATION_COST)
