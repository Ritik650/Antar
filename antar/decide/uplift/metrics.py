"""Uplift metrics: ranking, sign recovery, calibration, and rupees.

`docs/EVALUATION.md` §6.2, as amended 23 Aug 2026 before the bake-off ran.

## Why ranking metrics alone are not enough

Qini and AUUC measure whether a model *orders* customers well. They are the standard
uplift metrics and they are reported here. But a model can have excellent AUUC and get
the **sign** wrong at the bottom of the ranking — and the bottom of the ranking is
where Antar's entire thesis lives. The sleeping-dogs population is not "the customers
ranked last"; it is "the customers whose true uplift is below zero", and telling those
two apart is the difference between a ranking exercise and a product.

So the sign metrics below are co-primary with AUUC in selection, and the rupee value of
abstention is reported alongside, because "precision 0.71 on the negative region" only
becomes meaningful once you know what a correct abstention is worth.

## The asymmetry that makes abstention valuable

Abstaining on a truly-negative customer *earns* the harm you avoided. Abstaining on a
truly-positive one *costs* the uplift you gave up. Those are different magnitudes, and
`abstention_value` prices both rather than counting decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def qini_curve(
    scores: np.ndarray, treatment: np.ndarray, outcome: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative incremental outcomes as the population is targeted in score order.

    Returns (fraction targeted, cumulative incremental gain).
    """
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")
    t = np.asarray(treatment, dtype=float)[order]
    y = np.asarray(outcome, dtype=float)[order]

    treated_cum = np.cumsum(t * y)
    control_cum = np.cumsum((1 - t) * y)
    n_treated = np.cumsum(t)
    n_control = np.cumsum(1 - t)

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(n_control > 0, n_treated / np.maximum(n_control, 1), 0.0)
    gain = treated_cum - control_cum * ratio
    fraction = np.arange(1, len(order) + 1) / len(order)
    return fraction, gain


def qini_coefficient(scores: np.ndarray, treatment: np.ndarray, outcome: np.ndarray) -> float:
    """Area between the Qini curve and the random-targeting diagonal, normalised."""
    fraction, gain = qini_curve(scores, treatment, outcome)
    if len(gain) == 0 or gain[-1] == 0:
        return 0.0
    diagonal = gain[-1] * fraction
    area = float(np.trapezoid(gain - diagonal, fraction))
    return area / abs(gain[-1]) if gain[-1] else 0.0


def auuc(scores: np.ndarray, treatment: np.ndarray, outcome: np.ndarray) -> float:
    """Area under the uplift curve, normalised so batches of different sizes compare.

    Divided by the total incremental gain, so the result is the *shape* of the curve
    rather than its scale. Without that, a learner scored on a larger validation split
    would appear better for no reason other than size.
    """
    fraction, gain = qini_curve(scores, treatment, outcome)
    if len(gain) == 0:
        return 0.0
    total = abs(gain[-1])
    area = float(np.trapezoid(gain, fraction))
    return area / total if total > 1e-12 else 0.0


def qini_at(scores: np.ndarray, treatment: np.ndarray, outcome: np.ndarray, top: float = 0.2) -> float:
    """Qini gain at the top `top` of the ranking. Tie-break (a) in §6.2 step 5."""
    _fraction, gain = qini_curve(scores, treatment, outcome)
    if len(gain) == 0:
        return 0.0
    cutoff = int(max(1, round(top * len(gain))))
    return float(gain[cutoff - 1])


# ---------------------------------------------------------------------------
# Sign recovery - the co-primary criterion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignMetrics:
    """How well the model identifies the population it must not contact."""

    precision: float
    recall: float
    f1: float
    predicted_negative: int
    actually_negative: int
    true_positives: int
    population: int

    @property
    def prevalence(self) -> float:
        return self.actually_negative / self.population if self.population else 0.0

    @property
    def identified_share(self) -> float:
        """Of the population that exists, how much does the model find?

        The commercially meaningful number. A 36% population the model cannot
        identify is worth zero rupees; a 5.8% population it identifies cleanly is
        worth real money.
        """
        return self.recall

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "prevalence": round(self.prevalence, 5),
        }


def sign_metrics(predicted: np.ndarray, true_uplift: np.ndarray, *, threshold: float = 0.0) -> SignMetrics:
    """Precision / recall / F1 on `sign(uplift) < 0`."""
    predicted_neg = np.asarray(predicted, dtype=float) < threshold
    actual_neg = np.asarray(true_uplift, dtype=float) < threshold

    tp = int(np.sum(predicted_neg & actual_neg))
    fp = int(np.sum(predicted_neg & ~actual_neg))
    fn = int(np.sum(~predicted_neg & actual_neg))

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return SignMetrics(
        precision=round(precision, 5),
        recall=round(recall, 5),
        f1=round(f1, 5),
        predicted_negative=int(predicted_neg.sum()),
        actually_negative=int(actual_neg.sum()),
        true_positives=tp,
        population=len(predicted),
    )


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbstentionValue:
    """The rupee consequence of the model's abstention decisions.

    Abstaining on a truly-negative customer earns the harm avoided. Abstaining on a
    truly-positive one costs the uplift forgone. Counting decisions would treat those
    as equal; they are not.
    """

    saved_paise: int
    forgone_paise: int
    n_abstained: int
    n_correctly_abstained: int

    @property
    def net_paise(self) -> int:
        return self.saved_paise - self.forgone_paise

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "net_paise": self.net_paise,
            "net_rupees": round(self.net_paise / 100, 2),
        }


def abstention_value(
    predicted: np.ndarray,
    true_uplift: np.ndarray,
    amount_paise: np.ndarray,
    *,
    threshold: float = 0.0,
    optout_loss_multiplier: float = 6.0,
) -> AbstentionValue:
    """Price the abstention decisions the model would make.

    A negative true uplift means contacting reduces recovery probability. The harm
    avoided by staying silent is that reduction, valued at the cycle amount plus the
    lifetime value at risk when an opt-out ends the mandate - hence
    `optout_loss_multiplier`, which is the same coefficient the allocator's objective
    uses so the two cannot disagree.
    """
    predicted = np.asarray(predicted, dtype=float)
    true_uplift = np.asarray(true_uplift, dtype=float)
    amounts = np.asarray(amount_paise, dtype=float)

    abstained = predicted < threshold
    correctly = abstained & (true_uplift < 0)
    wrongly = abstained & (true_uplift >= 0)

    # Harm avoided: the (negative) uplift we did not incur, valued at the amount plus
    # the mandate at risk behind it.
    saved = float(np.sum(-true_uplift[correctly] * amounts[correctly] * optout_loss_multiplier))
    # Gain forgone: uplift we could have had, valued at the cycle only. Losing a cycle
    # is not the same as losing a customer.
    forgone = float(np.sum(true_uplift[wrongly] * amounts[wrongly]))

    return AbstentionValue(
        saved_paise=round(saved),
        forgone_paise=round(forgone),
        n_abstained=int(abstained.sum()),
        n_correctly_abstained=int(correctly.sum()),
    )


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    """Predicted vs realised uplift, by decile of the prediction."""

    deciles: list[dict[str, float]] = field(default_factory=list)
    negative_region_bias: float = 0.0
    """Mean(predicted - true) restricted to the decile the model ranks lowest.

    Reported separately because that is where the mass is thin, where every learner is
    least reliable, and where Antar acts on the estimate by *not acting*.
    """

    def as_dict(self) -> dict[str, Any]:
        return {
            "deciles": self.deciles,
            "negative_region_bias": round(self.negative_region_bias, 6),
        }


def calibration(predicted: np.ndarray, true_uplift: np.ndarray, *, bins: int = 10) -> Calibration:
    predicted = np.asarray(predicted, dtype=float)
    true_uplift = np.asarray(true_uplift, dtype=float)
    if len(predicted) < bins:
        return Calibration()

    order = np.argsort(predicted, kind="stable")
    chunks = np.array_split(order, bins)
    result = Calibration()
    for index, chunk in enumerate(chunks):
        result.deciles.append(
            {
                "decile": index,
                "n": len(chunk),
                "mean_predicted": round(float(predicted[chunk].mean()), 6),
                "mean_true": round(float(true_uplift[chunk].mean()), 6),
                "bias": round(float((predicted[chunk] - true_uplift[chunk]).mean()), 6),
            }
        )
    lowest = chunks[0]
    result.negative_region_bias = float((predicted[lowest] - true_uplift[lowest]).mean())
    return result


# ---------------------------------------------------------------------------
# The per-learner report
# ---------------------------------------------------------------------------


@dataclass
class LearnerReport:
    name: str
    auuc: float
    qini: float
    qini_at_20: float
    sign: SignMetrics
    abstention: AbstentionValue
    calibration_report: Calibration
    fit_seconds: float = 0.0
    disqualified: bool = False
    disqualification_reason: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "auuc": round(self.auuc, 6),
            "qini": round(self.qini, 6),
            "qini_at_20": round(self.qini_at_20, 4),
            "sign": self.sign.as_dict(),
            "abstention": self.abstention.as_dict(),
            "calibration": self.calibration_report.as_dict(),
            "fit_seconds": round(self.fit_seconds, 2),
            "disqualified": self.disqualified,
            "disqualification_reason": self.disqualification_reason,
            "notes": self.notes,
        }


def zscore(values: list[float]) -> list[float]:
    """Standardise across the candidate set. Constant input gives all zeros."""
    array = np.asarray(values, dtype=float)
    spread = array.std()
    if spread < 1e-12:
        return [0.0] * len(values)
    return [float(v) for v in (array - array.mean()) / spread]
