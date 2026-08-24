"""The four uplift learners. **No LLM.**

T-learner, X-learner, R-learner, and CausalForestDML, all behind
`antar.decide.uplift.base.UpliftEstimator`.

PLAN.md section 6 sketches one module per learner. They are gathered here instead
because each is 30-50 lines and the interesting content is the *contrast* between them,
which is easier to read on one page than across four files. ADR-0014.

## What distinguishes them, in one line each

  * **T-learner** — fit two outcome models, subtract. Simple, and biased toward
    whichever arm has more data, which here is the untreated one by a wide margin.
  * **X-learner** — impute each unit's counterfactual from the *other* arm's model,
    then regress the imputed effects. Designed for exactly our imbalance.
  * **R-learner** — residualise outcome and treatment on X, then regress one residual
    on the other. Robust to a badly-specified outcome model; needs a propensity, and
    ours is **known** rather than estimated (docs/EVALUATION.md §7.1.1).
  * **CausalForestDML** — econml's honest causal forest. Most flexible, most data
    hungry, and the one PLAN.md M5 timeboxes.

Every learner must be able to output a **negative** uplift. That is not an edge case
here; it is the population the product is built around.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor

from antar.decide.uplift.base import FittedGuard, align, encode

DEFAULT_TREE_PARAMS: dict[str, Any] = {
    "n_estimators": 200,
    "max_depth": 4,
    "learning_rate": 0.05,
    "min_samples_leaf": 40,
    "random_state": 20260822,
}


def _classifier(**overrides: Any) -> GradientBoostingClassifier:
    params = {**DEFAULT_TREE_PARAMS, **overrides}
    return GradientBoostingClassifier(**params)


def _regressor(**overrides: Any) -> GradientBoostingRegressor:
    params = {**DEFAULT_TREE_PARAMS, **overrides}
    return GradientBoostingRegressor(**params)


def _predict_proba(model: Any, X: pd.DataFrame) -> np.ndarray:
    """P(outcome = 1), tolerating a model fitted on a single-class arm.

    A thin treatment arm can easily contain only non-recoveries. scikit-learn happily
    fits that and then returns a one-column probability matrix, which silently
    broadcasts into nonsense if you index `[:, 1]` without checking.
    """
    proba = model.predict_proba(X)
    if proba.shape[1] == 1:
        return np.full(len(X), float(model.classes_[0]))
    return proba[:, 1]


# ---------------------------------------------------------------------------


class TLearner(FittedGuard):
    """Two outcome models, subtracted.

    The baseline every uplift comparison needs. Its weakness here is structural: the
    untreated arm has roughly five times the data of the treated one, so the treated
    model is far noisier, and the difference inherits that noise directly.
    """

    name = "t_learner"

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.model_treated = _classifier(**params)
        self.model_control = _classifier(**params)
        self._columns: pd.DataFrame | None = None

    def fit(self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **_: Any) -> TLearner:
        encoded = encode(X)
        self._columns = encoded.iloc[:0]
        treated = treatment == 1
        self.model_treated.fit(encoded[treated], outcome[treated])
        self.model_control.fit(encoded[~treated], outcome[~treated])
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        assert self._columns is not None
        encoded = align(self._columns, encode(X))
        return _predict_proba(self.model_treated, encoded) - _predict_proba(
            self.model_control, encoded
        )


class XLearner(FittedGuard):
    """Impute counterfactuals across arms, then regress the imputed effects.

    Built for imbalanced treatment assignment, which is our situation exactly: the
    control model is estimated on plenty of data and used to impute what the treated
    units would have done, so the thin treated arm is not asked to carry the whole
    estimate on its own.

    The propensity used to blend the two effect models is **known**, not estimated
    (docs/EVALUATION.md §7.1.1), so `g` is passed in rather than fitted.
    """

    name = "x_learner"

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.outcome_treated = _classifier(**params)
        self.outcome_control = _classifier(**params)
        self.effect_treated = _regressor(**params)
        self.effect_control = _regressor(**params)
        self._columns: pd.DataFrame | None = None
        self._propensity: float = 0.5

    def fit(
        self,
        X: pd.DataFrame,
        treatment: np.ndarray,
        outcome: np.ndarray,
        *,
        propensity: np.ndarray | float | None = None,
        **_: Any,
    ) -> XLearner:
        encoded = encode(X)
        self._columns = encoded.iloc[:0]
        treated = treatment == 1

        self.outcome_treated.fit(encoded[treated], outcome[treated])
        self.outcome_control.fit(encoded[~treated], outcome[~treated])

        # Imputed individual effects, each estimated using the *other* arm's model.
        d_treated = outcome[treated] - _predict_proba(self.outcome_control, encoded[treated])
        d_control = _predict_proba(self.outcome_treated, encoded[~treated]) - outcome[~treated]

        self.effect_treated.fit(encoded[treated], d_treated)
        self.effect_control.fit(encoded[~treated], d_control)

        if propensity is None:
            self._propensity = float(treated.mean())
        else:
            self._propensity = float(np.mean(propensity))
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        assert self._columns is not None
        encoded = align(self._columns, encode(X))
        g = self._propensity
        # Weighting by the propensity leans on whichever effect model was fitted on
        # more data, which is the point of the X-learner.
        return g * self.effect_control.predict(encoded) + (1 - g) * self.effect_treated.predict(
            encoded
        )


class RLearner(FittedGuard):
    """Robinson residualisation: regress outcome residuals on treatment residuals.

    Needs a propensity and an outcome model. Ours is the case the R-learner was built
    for and rarely gets: the propensity is **exact**, so the only misspecification risk
    left is in the outcome model - which is precisely what the §7.3 consistency check
    is designed to detect.
    """

    name = "r_learner"

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.outcome_model = _regressor(**params)
        self.effect_model = _regressor(**params)
        self._columns: pd.DataFrame | None = None

    def fit(
        self,
        X: pd.DataFrame,
        treatment: np.ndarray,
        outcome: np.ndarray,
        *,
        propensity: np.ndarray | float | None = None,
        **_: Any,
    ) -> RLearner:
        encoded = encode(X)
        self._columns = encoded.iloc[:0]

        self.outcome_model.fit(encoded, outcome)
        outcome_residual = outcome - self.outcome_model.predict(encoded)

        if propensity is None:
            treatment_hat = np.full(len(treatment), float(treatment.mean()))
        else:
            treatment_hat = np.broadcast_to(np.asarray(propensity, dtype=float), treatment.shape)
        treatment_residual = treatment - treatment_hat

        # Weighted regression of the pseudo-outcome. Weights are the squared treatment
        # residual, so units whose assignment was nearly deterministic - and therefore
        # carry almost no causal information - contribute almost nothing.
        weights = treatment_residual**2
        safe = np.abs(treatment_residual) > 1e-8
        if safe.sum() < 10:
            raise ValueError(
                "too few units with non-degenerate treatment residuals to fit an "
                "R-learner; the exploration split is too small or too unbalanced"
            )
        pseudo = np.zeros_like(outcome_residual, dtype=float)
        pseudo[safe] = outcome_residual[safe] / treatment_residual[safe]

        self.effect_model.fit(encoded[safe], pseudo[safe], sample_weight=weights[safe])
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        assert self._columns is not None
        return self.effect_model.predict(align(self._columns, encode(X)))


class CausalForest(FittedGuard):
    """econml's `CausalForestDML`. The most flexible and the most data-hungry.

    PLAN.md M5 puts a hard timebox on this one: if it has not converged by the morning
    of 1 Sep, ship the X-learner and record the reason in an ADR. `fit` therefore fails
    loudly rather than degrading, so a non-convergence is a visible event rather than a
    quietly worse model.
    """

    name = "causal_forest"

    def __init__(self, **params: Any) -> None:
        self.params = {
            "n_estimators": params.get("n_estimators", 500),
            "min_samples_leaf": params.get("min_samples_leaf", 30),
            "max_depth": params.get("max_depth"),
            "random_state": params.get("random_state", 20260822),
        }
        self._model: Any = None
        self._columns: pd.DataFrame | None = None

    def fit(
        self,
        X: pd.DataFrame,
        treatment: np.ndarray,
        outcome: np.ndarray,
        *,
        propensity: np.ndarray | float | None = None,
        **_: Any,
    ) -> CausalForest:
        from econml.dml import CausalForestDML

        encoded = encode(X)
        self._columns = encoded.iloc[:0]
        self._model = CausalForestDML(
            model_y=_regressor(),
            model_t=_classifier(),
            discrete_treatment=True,
            cv=3,
            **self.params,
        )
        self._model.fit(Y=outcome, T=treatment, X=encoded.to_numpy(dtype=float))
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        assert self._columns is not None
        encoded = align(self._columns, encode(X))
        return np.asarray(self._model.effect(encoded.to_numpy(dtype=float))).ravel()


# ---------------------------------------------------------------- baselines


class RandomTargeting(FittedGuard):
    """Baseline: a random score. The floor any learner must clear."""

    name = "random"

    def __init__(self, seed: int = 20260822, **_: Any) -> None:
        self.seed = seed

    def fit(self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **_: Any):
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        rng = np.random.default_rng(self.seed)
        return rng.normal(0.0, 0.01, size=len(X))


class PropensityTargeting(FittedGuard):
    """Baseline: target by predicted probability of recovery. **The sophisticated
    wrong answer**, and the comparison the whole evaluation is built around.

    docs/EVALUATION.md §8: beating "contact everyone" is easy and proves little.
    Beating a competent propensity model is the claim worth making, because targeting
    the *likely to recover* is not the same as targeting the *persuadable* - and this
    baseline is what makes that distinction measurable rather than rhetorical.

    Note what it cannot do: its score is a probability in [0, 1], so it can never be
    negative and therefore can never recommend abstention. That is not a handicap we
    imposed; it is the actual limitation of the approach.
    """

    name = "propensity"

    def __init__(self, **params: Any) -> None:
        self.model = _classifier(**params)
        self._columns: pd.DataFrame | None = None

    def fit(self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **_: Any):
        encoded = encode(X)
        self._columns = encoded.iloc[:0]
        self.model.fit(encoded, outcome)
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        assert self._columns is not None
        return _predict_proba(self.model, align(self._columns, encode(X)))


class AlwaysAbstain(FittedGuard):
    """Baseline: declare every customer negative. **The floor for sign recovery.**

    Added after the first bake-off run, because the sign-F1 numbers were suspicious.
    At prevalence p, a constant "everyone is negative" predictor scores precision p,
    recall 1.0, and therefore F1 = 2p/(1+p) - which at our prevalence is 0.26, higher
    than any learner achieved. Without this baseline in the table that fact is
    invisible, and a reader would take an F1 of 0.24 as evidence of something.

    It is included precisely because it embarrasses the learners. Its abstention value
    is the counterweight: abstaining on everybody forgoes every positive uplift in the
    batch, so the rupee metric separates it decisively from a model that abstains
    selectively. That contrast is the argument for denominating the metric in money.
    """

    name = "always_abstain"

    def __init__(self, **_: Any) -> None:
        pass

    def fit(self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **_: Any):
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        return np.full(len(X), -1.0)


class AlwaysContact(FittedGuard):
    """Baseline: contact everyone. The industry default, and policy P1 in EVALUATION 8.

    Never abstains, so its sign recall is 0 by construction and its abstention value
    is exactly zero. Beating it is easy and proves little; it is here so the easy
    comparison is on the same table as the hard one.
    """

    name = "always_contact"

    def __init__(self, **_: Any) -> None:
        pass

    def fit(self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **_: Any):
        self._fitted = True
        return self

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray:
        self._require_fitted()
        return np.full(len(X), 1.0)


class OptoutRisk:
    """The harm model. A treated-arm response model, not an uplift learner.

    An X-learner cannot be fitted to opt-out in this data, and the reason is worth
    stating plainly: **no untreated customer in the simulator ever opts out.** Measured
    on the base scenario, seed 7 - 0 opt-outs in 2,996 untreated rows against 35 in 260
    treated. The control arm has one class, so a two-arm learner has nothing to
    difference.

    That is a fact about the simulator, not about India. `SIMULATOR_CARD.md` models
    opt-out as a hazard triggered by contact, so spontaneous cancellation is not
    represented at all. Real customers cancel subscriptions on Sunday afternoons for
    reasons no merchant caused.

    So this estimator does the only defensible thing: it fits `P(optout | X, treated)`
    on the treated arm and subtracts the **measured** untreated rate, rather than
    assuming that rate is zero. On this data the subtraction is a no-op, and if the
    simulator ever grows spontaneous churn the same code estimates it properly. The
    limitation is recorded as `docs/LIMITATIONS.md` L17, because an opt-out cost that is
    only a causal effect by construction is not the same claim as one identified from
    data.
    """

    def __init__(self) -> None:
        self.model: Any = None
        self.baseline_rate: float = 0.0
        self.treated_rows: int = 0

    def fit(self, X: Any, treated: Any, outcome: Any) -> OptoutRisk:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier

        treated = np.asarray(treated).astype(bool)
        outcome = np.asarray(outcome).astype(int)

        untreated_outcomes = outcome[~treated]
        self.baseline_rate = (
            float(untreated_outcomes.mean()) if untreated_outcomes.size else 0.0
        )

        encoded = encode(X)
        self.reference = encoded
        treated_X = encoded[treated]
        treated_y = outcome[treated]
        self.treated_rows = int(treated.sum())

        if treated_y.size == 0 or len(np.unique(treated_y)) < 2:
            # Nobody opted out under treatment either. Predict the observed rate rather
            # than refusing: a constant of zero is the correct estimate when the outcome
            # never occurred, and it is honest about carrying no information.
            self.model = None
            self.constant = float(treated_y.mean()) if treated_y.size else 0.0
            return self

        self.model = GradientBoostingClassifier(random_state=0)
        self.model.fit(treated_X, treated_y)
        return self

    def predict_uplift(self, X: Any) -> Any:
        encoded = align(self.reference, encode(X))
        if self.model is None:
            return np.full(len(encoded), max(0.0, self.constant - self.baseline_rate))
        treated_risk = self.model.predict_proba(encoded)[:, 1]
        return treated_risk - self.baseline_rate


LEARNERS: dict[str, type] = {
    "t_learner": TLearner,
    "x_learner": XLearner,
    "r_learner": RLearner,
    "causal_forest": CausalForest,
}

BASELINES: dict[str, type] = {
    "random": RandomTargeting,
    "propensity": PropensityTargeting,
    "always_abstain": AlwaysAbstain,
    "always_contact": AlwaysContact,
}

ALL_MODELS: dict[str, type] = {**LEARNERS, **BASELINES}
