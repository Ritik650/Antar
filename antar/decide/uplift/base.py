"""The `UpliftEstimator` protocol. **No LLM in this package.**

Every learner in `antar/decide/uplift/` implements this and nothing more. The narrow
surface is deliberate: the bake-off harness and the allocator both work against the
protocol, so swapping a learner is a one-line change and no learner can quietly acquire
a special path through the system.

## What an uplift estimator is being asked for

Not `P(recover | treated)`. The difference:

    tau(x) = P(recover | treated, x) - P(recover | untreated, x)

which is the quantity that decides whether contacting this customer *adds* anything.
A customer with a 90% recovery probability either way has `tau ≈ 0` and is worth no
contact at all, however attractive their recovery probability looks.

`tau` may be **negative**. That is not an artefact to be clipped away - it is the
sleeping-dogs population, and it is the reason this system exists. Any learner that
cannot represent a negative `tau` is disqualified before it starts.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd


@runtime_checkable
class UpliftEstimator(Protocol):
    """Fit on (X, treatment, outcome); predict per-row conditional treatment effect."""

    name: str

    def fit(
        self, X: pd.DataFrame, treatment: np.ndarray, outcome: np.ndarray, **kwargs: Any
    ) -> UpliftEstimator: ...

    def predict_uplift(self, X: pd.DataFrame) -> np.ndarray: ...


def encode(X: pd.DataFrame) -> pd.DataFrame:
    """One-hot the categoricals and return a numeric matrix.

    Shared by every learner so that a bake-off comparison is between *methods* rather
    than between accidentally different encodings. econml's estimators want plain
    numeric arrays, and LightGBM's native categorical handling would give the tree-based
    learners a representational advantage the linear ones do not get.
    """
    return pd.get_dummies(X, drop_first=False, dtype=float)


def align(reference: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    """Align `other`'s columns to `reference`'s, filling absent levels with zero.

    A validation split can easily lack an issuer that the training split had. Without
    this, the matrices differ in width and the model either raises or - worse - scores
    against shifted columns.
    """
    return other.reindex(columns=reference.columns, fill_value=0.0)


class FittedGuard:
    """Mixin: refuse to predict before fitting.

    PLAN.md section 10, last row: "Uplift model missing / version mismatch -> refuses to
    act; does not silently fall back to targeting everyone." A model that returns zeros
    when unfitted produces a policy that contacts nobody, or everybody, depending on the
    comparison - and does so without complaining.
    """

    _fitted: bool = False

    def _require_fitted(self) -> None:
        if not self._fitted:
            raise RuntimeError(
                f"{type(self).__name__} is not fitted; refusing to predict. A money "
                "system that silently returns a default uplift is worse than one that "
                "stops."
            )
