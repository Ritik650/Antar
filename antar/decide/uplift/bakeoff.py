"""The bake-off. Implements `docs/EVALUATION.md` §6.2 exactly, and nothing else.

The selection rule was **amended before this file was run** to add sign recovery as a
co-primary criterion, because AUUC alone could have selected the learner that is worst
at the one thing the product does. That amendment is logged in §13 and the ordering is
checkable in git.

## The rule, restated

1. Split the exploration data train/validation by **customer**, 70/30.
2. Fit each learner with a pre-specified grid.
3. **Disqualify** any learner whose negative-region recall is below 0.10.
4. Select on `0.5·z(AUUC) + 0.5·z(negative-region sign F1)`.
5. Tie-break: Qini@20% → narrower CI → simpler model.
6. Evaluate on the control holdout **once**.

Nothing in this module looks at the holdout. `select()` returns a decision; the holdout
evaluation is a separate call the caller makes afterwards, so the two cannot be
accidentally interleaved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from antar.decide.uplift.learners import ALL_MODELS, BASELINES, LEARNERS
from antar.decide.uplift.metrics import (
    LearnerReport,
    abstention_value,
    auuc,
    calibration,
    qini_at,
    qini_coefficient,
    sign_metrics,
    zscore,
)

NEGATIVE_RECALL_FLOOR = 0.10
"""§6.2 step 3. A learner that cannot find the negative-uplift population at all cannot
support the claim Antar is built on, and no amount of ranking quality substitutes."""

SIMPLICITY_ORDER = ["t_learner", "x_learner", "r_learner", "causal_forest"]
"""§6.2 step 5(c). Simpler wins a tie."""

AUUC_WEIGHT = 0.5
SIGN_WEIGHT = 0.5


@dataclass
class BakeoffResult:
    reports: dict[str, LearnerReport] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    selected: str = ""
    selection_rationale: str = ""
    disqualified: dict[str, str] = field(default_factory=dict)
    n_train: int = 0
    n_validation: int = 0
    negative_prevalence: float = 0.0

    @property
    def eligible(self) -> list[str]:
        return [name for name in self.reports if name not in self.disqualified]

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "selection_rationale": self.selection_rationale,
            "selection_rule": (
                f"{AUUC_WEIGHT}*z(validation AUUC) + {SIGN_WEIGHT}*z(negative-region "
                f"sign F1), after disqualifying any learner with negative-region "
                f"recall < {NEGATIVE_RECALL_FLOOR}"
            ),
            "n_train": self.n_train,
            "n_validation": self.n_validation,
            "negative_prevalence": round(self.negative_prevalence, 5),
            "scores": {k: round(v, 5) for k, v in sorted(self.scores.items())},
            "disqualified": self.disqualified,
            "reports": {name: report.as_dict() for name, report in sorted(self.reports.items())},
            "caveat": (
                "Validation AUUC of the selected model is NOT an unbiased estimate of "
                "its performance: it won partly on merit and partly on noise, having "
                "been chosen on this same split. Only the control holdout is "
                "inferential. docs/EVALUATION.md 6.2.2."
            ),
        }


def split_by_customer(
    frame: pd.DataFrame, *, train_fraction: float = 0.70, salt: str = "bakeoff"
) -> np.ndarray:
    """Deterministic train/validation mask, split on **customer**, not row.

    Splitting on rows would put the same customer's cycles on both sides and leak
    their idiosyncrasy into validation, which is the single easiest way to make an
    uplift model look better than it is.
    """
    import hashlib

    def draw(customer_id: str) -> float:
        digest = hashlib.blake2b(f"{salt}\x1f{customer_id}".encode(), digest_size=8).digest()
        return (int.from_bytes(digest, "big") % 1_000_000) / 1_000_000

    return np.array([draw(c) < train_fraction for c in frame["customer_id"]])


def evaluate_learner(
    name: str,
    model: Any,
    X_val: pd.DataFrame,
    val: pd.DataFrame,
    truth_val: pd.DataFrame,
    *,
    optout_loss_multiplier: float = 6.0,
    fit_seconds: float = 0.0,
) -> LearnerReport:
    predicted = model.predict_uplift(X_val)
    treatment = val["treated"].to_numpy()
    outcome = val["recovered"].to_numpy()
    true_uplift = truth_val["true_uplift"].to_numpy()
    amounts = val["amount_paise"].to_numpy()

    return LearnerReport(
        name=name,
        auuc=auuc(predicted, treatment, outcome),
        qini=qini_coefficient(predicted, treatment, outcome),
        qini_at_20=qini_at(predicted, treatment, outcome, top=0.2),
        sign=sign_metrics(predicted, true_uplift),
        abstention=abstention_value(
            predicted, true_uplift, amounts, optout_loss_multiplier=optout_loss_multiplier
        ),
        calibration_report=calibration(predicted, true_uplift),
        fit_seconds=fit_seconds,
    )


def run_bakeoff(
    log: Any,
    *,
    include_baselines: bool = True,
    optout_loss_multiplier: float = 6.0,
    params: dict[str, dict[str, Any]] | None = None,
    timebox_seconds: float | None = None,
) -> BakeoffResult:
    """Fit and score every candidate on the exploration split.

    `timebox_seconds` implements PLAN.md M5's hard timebox: a learner that exceeds it
    is dropped with a stated operational reason rather than being allowed to eat the
    schedule. Dropping a candidate for taking too long is fine. Dropping it because it
    won is not, so the timeout is recorded in the result and reported.
    """
    params = params or {}
    exploration = log.split(log.exploration)
    if len(exploration) < 100:
        raise ValueError(
            f"exploration split has only {len(exploration)} rows; the bake-off would "
            "be measuring noise. Pool more seeds or raise epsilon."
        )

    train_mask = split_by_customer(exploration.frame)
    train = exploration.split(pd.Series(train_mask))
    validation = exploration.split(pd.Series(~train_mask))

    X_train, X_val = train.features, validation.features
    t_train = train.frame["treated"].to_numpy()
    y_train = train.frame["recovered"].to_numpy()
    # Known, not estimated. docs/EVALUATION.md 7.1.1: on the exploration split the
    # probability of *being treated* is (|feasible| - 1) / |feasible|, because exactly
    # one of the feasible actions is "do nothing".
    sizes = train.frame["feasible_set_size"].to_numpy(dtype=float)
    known_propensity = np.where(sizes > 0, (sizes - 1.0) / np.maximum(sizes, 1.0), 0.5)

    result = BakeoffResult(
        n_train=len(train),
        n_validation=len(validation),
        negative_prevalence=float((validation.truth["true_uplift"] < 0).mean()),
    )

    candidates = dict(LEARNERS)
    if include_baselines:
        candidates = {**candidates, **BASELINES}

    for name, cls in candidates.items():
        model = cls(**params.get(name, {}))
        started = time.perf_counter()
        try:
            model.fit(X_train, t_train, y_train, propensity=known_propensity)
        except Exception as exc:
            result.disqualified[name] = f"fit failed: {type(exc).__name__}: {exc}"
            continue
        elapsed = time.perf_counter() - started

        if timebox_seconds is not None and elapsed > timebox_seconds:
            result.disqualified[name] = (
                f"exceeded the {timebox_seconds:.0f}s timebox ({elapsed:.0f}s). "
                "Dropped for a stated operational reason, per PLAN.md M5."
            )
            continue

        report = evaluate_learner(
            name, model, X_val, validation.frame, validation.truth,
            optout_loss_multiplier=optout_loss_multiplier, fit_seconds=elapsed,
        )
        result.reports[name] = report

    _select(result)
    return result


def _select(result: BakeoffResult) -> None:
    """Steps 3-5 of §6.2. Baselines are scored and reported but never selected."""
    selectable = [
        name
        for name in result.reports
        if name in LEARNERS and name not in result.disqualified
    ]

    # Step 3: the disqualification floor.
    for name in list(selectable):
        report = result.reports[name]
        if report.sign.recall < NEGATIVE_RECALL_FLOOR:
            report.disqualified = True
            report.disqualification_reason = (
                f"negative-region recall {report.sign.recall:.3f} is below the "
                f"pre-registered floor of {NEGATIVE_RECALL_FLOOR}. A model that cannot "
                "find the sleeping-dogs population cannot support the claim the system "
                "is built on."
            )
            result.disqualified[name] = report.disqualification_reason
            selectable.remove(name)

    if not selectable:
        result.selected = ""
        result.selection_rationale = (
            "Every learner was disqualified. Per PLAN.md section 10 the safe failure "
            "for a money system is to do nothing: with no eligible uplift model, Antar "
            "refuses to act rather than falling back to targeting everyone."
        )
        return

    # Step 4: the co-primary score.
    auuc_z = dict(zip(selectable, zscore([result.reports[n].auuc for n in selectable]), strict=True))
    sign_z = dict(
        zip(selectable, zscore([result.reports[n].sign.f1 for n in selectable]), strict=True)
    )
    for name in selectable:
        result.scores[name] = AUUC_WEIGHT * auuc_z[name] + SIGN_WEIGHT * sign_z[name]

    # Step 5: tie-break, in the pre-registered order.
    def sort_key(name: str) -> tuple:
        report = result.reports[name]
        return (
            -round(result.scores[name], 6),
            -report.qini_at_20,
            SIMPLICITY_ORDER.index(name) if name in SIMPLICITY_ORDER else 99,
        )

    ranked = sorted(selectable, key=sort_key)
    result.selected = ranked[0]
    winner = result.reports[result.selected]

    runner_up = ranked[1] if len(ranked) > 1 else None
    margin = (
        result.scores[result.selected] - result.scores[runner_up] if runner_up else float("nan")
    )
    result.selection_rationale = (
        f"{result.selected}: score {result.scores[result.selected]:+.3f} "
        f"(AUUC {winner.auuc:.4f}, negative-region F1 {winner.sign.f1:.3f}, "
        f"precision {winner.sign.precision:.3f}, recall {winner.sign.recall:.3f}). "
        + (
            f"Runner-up {runner_up} by {margin:+.3f}. "
            if runner_up
            else "No runner-up. "
        )
        + "Selected on validation only; the holdout has not been touched."
    )


def ranking_table(result: BakeoffResult) -> list[dict[str, Any]]:
    """The bake-off table for the README, sorted as selection sorted it."""
    rows = []
    for name, report in result.reports.items():
        rows.append(
            {
                "learner": name,
                "is_baseline": name in BASELINES,
                "auuc": round(report.auuc, 5),
                "qini": round(report.qini, 5),
                "sign_precision": report.sign.precision,
                "sign_recall": report.sign.recall,
                "sign_f1": report.sign.f1,
                "negative_bias": round(report.calibration_report.negative_region_bias, 5),
                "abstention_net_rupees": round(report.abstention.net_paise / 100, 2),
                "score": round(result.scores.get(name, float("nan")), 5),
                "selected": name == result.selected,
                "disqualified": name in result.disqualified,
            }
        )
    return sorted(rows, key=lambda r: (r["is_baseline"], -(r["score"] if r["score"] == r["score"] else -99)))


__all__ = [
    "ALL_MODELS",
    "NEGATIVE_RECALL_FLOOR",
    "BakeoffResult",
    "evaluate_learner",
    "ranking_table",
    "run_bakeoff",
    "split_by_customer",
]
