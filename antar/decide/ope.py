"""Off-policy evaluation: IPS, SNIPS, and doubly-robust. **No LLM.**

`docs/EVALUATION.md` §7. Lets a candidate policy be scored without re-running the world,
which matters because re-running the world is the one thing a real merchant cannot do.

## Propensities are known here

§7.1.1. On the exploration split the action was drawn by a design we control, so the
propensity is a **design parameter** rather than something to be recovered:

    P(treated) = 0.5,  P(this channel | treated) = 1 / |feasible contacts|

Antar fits no propensity model on that split. This is not a small convenience.
Off-policy evaluation usually goes wrong through propensity misspecification, and every
diagnostic below exists to detect that failure - so removing it entirely makes the
§7.3 consistency check *diagnostic*: if DR and the on-policy holdout disagree, the
problem is in the **outcome model**, because the propensity side is exact.

## The diagnostics are mandatory, not optional

§7.2. An importance-weighted estimate with an effective sample size of 40 is a number
with a confidence interval and no information in it. Every estimate below carries its
ESS, its maximum weight, its 99th-percentile weight, and the fraction clipped - and
`OPEResult.reliable` is False when ESS falls under 10% of nominal, so a caller cannot
quote the point estimate without also seeing that it should not be quoted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

DEFAULT_CLIP = 20.0
"""Maximum importance weight. Clipping trades a little bias for a lot of variance, and
an unclipped weight of 500 on one lucky row is not an estimate, it is that row."""

ESS_RELIABILITY_FLOOR = 0.10
"""§7.2: below 10% of nominal, the estimate is reported as unreliable and the on-policy
holdout stands alone."""


@dataclass
class WeightDiagnostics:
    effective_sample_size: float
    nominal_sample_size: int
    max_weight: float
    p99_weight: float
    clipped_fraction: float
    clip_threshold: float

    @property
    def ess_ratio(self) -> float:
        return self.effective_sample_size / self.nominal_sample_size if self.nominal_sample_size else 0.0

    @property
    def reliable(self) -> bool:
        return self.ess_ratio >= ESS_RELIABILITY_FLOOR

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "ess_ratio": round(self.ess_ratio, 5),
            "reliable": self.reliable,
        }


@dataclass
class OPEResult:
    estimator: str
    estimate: float
    diagnostics: WeightDiagnostics
    note: str = ""

    @property
    def reliable(self) -> bool:
        return self.diagnostics.reliable

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimator": self.estimator,
            "estimate": round(self.estimate, 6),
            "reliable": self.reliable,
            "diagnostics": self.diagnostics.as_dict(),
            "note": self.note
            or (
                ""
                if self.reliable
                else (
                    f"UNRELIABLE: effective sample size is "
                    f"{self.diagnostics.ess_ratio:.1%} of nominal, below the "
                    f"{ESS_RELIABILITY_FLOOR:.0%} floor. Per docs/EVALUATION.md 7.2 "
                    "this estimate is not to be quoted; the on-policy holdout result "
                    "stands alone."
                )
            ),
        }


def importance_weights(
    target_action: np.ndarray,
    logged_action: np.ndarray,
    logged_propensity: np.ndarray,
    *,
    clip: float = DEFAULT_CLIP,
) -> tuple[np.ndarray, WeightDiagnostics]:
    """w_i = 1{target == logged} / propensity, clipped.

    The indicator is the deterministic-target-policy case, which is what Antar's
    allocator produces: it picks one action, not a distribution over them.
    """
    logged_propensity = np.asarray(logged_propensity, dtype=float)
    if np.any(logged_propensity <= 0):
        raise ValueError(
            "a logged propensity of zero means an action was taken that the logging "
            "policy could never have taken. That is a bookkeeping error, not a small "
            "one: every importance weight in the batch is suspect."
        )

    agrees = np.asarray(target_action) == np.asarray(logged_action)
    raw = np.where(agrees, 1.0 / logged_propensity, 0.0)
    clipped = np.minimum(raw, clip)

    nonzero = raw[raw > 0]
    diagnostics = WeightDiagnostics(
        effective_sample_size=_ess(clipped),
        nominal_sample_size=len(raw),
        max_weight=float(raw.max()) if len(raw) else 0.0,
        p99_weight=float(np.percentile(nonzero, 99)) if len(nonzero) else 0.0,
        clipped_fraction=float((raw > clip).mean()) if len(raw) else 0.0,
        clip_threshold=clip,
    )
    return clipped, diagnostics


def _ess(weights: np.ndarray) -> float:
    """Kish's effective sample size. The honest denominator for a weighted estimate."""
    total = weights.sum()
    if total <= 0:
        return 0.0
    return float(total**2 / np.sum(weights**2))


def ips(
    reward: np.ndarray,
    target_action: np.ndarray,
    logged_action: np.ndarray,
    logged_propensity: np.ndarray,
    *,
    clip: float = DEFAULT_CLIP,
) -> OPEResult:
    """Inverse propensity scoring. Unbiased, and high variance."""
    weights, diagnostics = importance_weights(
        target_action, logged_action, logged_propensity, clip=clip
    )
    estimate = float(np.mean(weights * np.asarray(reward, dtype=float)))
    return OPEResult("ips", estimate, diagnostics)


def snips(
    reward: np.ndarray,
    target_action: np.ndarray,
    logged_action: np.ndarray,
    logged_propensity: np.ndarray,
    *,
    clip: float = DEFAULT_CLIP,
) -> OPEResult:
    """Self-normalised IPS. Biased, lower variance, and bounded by the reward range -
    which IPS is not, and which is why an IPS estimate can exceed the largest reward
    that actually occurred."""
    weights, diagnostics = importance_weights(
        target_action, logged_action, logged_propensity, clip=clip
    )
    total = weights.sum()
    estimate = (
        float(np.sum(weights * np.asarray(reward, dtype=float)) / total) if total > 0 else 0.0
    )
    return OPEResult("snips", estimate, diagnostics)


def doubly_robust(
    reward: np.ndarray,
    target_action: np.ndarray,
    logged_action: np.ndarray,
    logged_propensity: np.ndarray,
    predicted_reward_target: np.ndarray,
    predicted_reward_logged: np.ndarray,
    *,
    clip: float = DEFAULT_CLIP,
) -> OPEResult:
    """Doubly robust: the outcome model plus an importance-weighted correction.

    Consistent if *either* the propensity model or the outcome model is right. Ours is
    the unusual case where the propensity is right by construction, so DR here is
    testing the outcome model and nothing else.
    """
    weights, diagnostics = importance_weights(
        target_action, logged_action, logged_propensity, clip=clip
    )
    reward = np.asarray(reward, dtype=float)
    q_target = np.asarray(predicted_reward_target, dtype=float)
    q_logged = np.asarray(predicted_reward_logged, dtype=float)

    estimate = float(np.mean(q_target + weights * (reward - q_logged)))
    return OPEResult(
        "doubly_robust",
        estimate,
        diagnostics,
        note=(
            "Propensities are known by design, not estimated (EVALUATION 7.1.1), so a "
            "disagreement between this and the on-policy holdout localises to the "
            "outcome model."
        ),
    )


@dataclass
class OPEComparison:
    """All three estimators, side by side. §7.1: DR is the headline, the others are
    reported so the reader can see how much the answer depends on the estimator."""

    results: dict[str, OPEResult] = field(default_factory=dict)
    on_policy: float | None = None
    on_policy_ci: tuple[float, float] | None = None

    @property
    def consistent(self) -> bool | None:
        """§7.3. Does the DR estimate fall inside the on-policy holdout's interval?

        `None` when there is nothing to compare against. A `False` is a **finding**,
        not an embarrassment - it means the outcome model is misspecified, and hiding a
        disagreement between two estimators is exactly the behaviour the protocol
        exists to prevent.
        """
        dr = self.results.get("doubly_robust")
        if dr is None or self.on_policy_ci is None:
            return None
        low, high = self.on_policy_ci
        return low <= dr.estimate <= high

    def as_dict(self) -> dict[str, Any]:
        verdict = self.consistent
        return {
            "estimates": {name: r.as_dict() for name, r in sorted(self.results.items())},
            "on_policy": self.on_policy,
            "on_policy_ci": list(self.on_policy_ci) if self.on_policy_ci else None,
            "dr_within_on_policy_ci": verdict,
            "consistency_note": (
                ""
                if verdict is None
                else (
                    "DR agrees with the on-policy holdout."
                    if verdict
                    else (
                        "DR falls OUTSIDE the on-policy CI. Per EVALUATION 7.3 this is "
                        "a finding to investigate and write up, not to hide: with the "
                        "propensity exact by construction, the disagreement points at "
                        "the outcome model."
                    )
                )
            ),
        }


def compare(
    reward: np.ndarray,
    target_action: np.ndarray,
    logged_action: np.ndarray,
    logged_propensity: np.ndarray,
    *,
    predicted_reward_target: np.ndarray | None = None,
    predicted_reward_logged: np.ndarray | None = None,
    on_policy: float | None = None,
    on_policy_ci: tuple[float, float] | None = None,
    clip: float = DEFAULT_CLIP,
) -> OPEComparison:
    comparison = OPEComparison(on_policy=on_policy, on_policy_ci=on_policy_ci)
    comparison.results["ips"] = ips(
        reward, target_action, logged_action, logged_propensity, clip=clip
    )
    comparison.results["snips"] = snips(
        reward, target_action, logged_action, logged_propensity, clip=clip
    )
    if predicted_reward_target is not None and predicted_reward_logged is not None:
        comparison.results["doubly_robust"] = doubly_robust(
            reward,
            target_action,
            logged_action,
            logged_propensity,
            predicted_reward_target,
            predicted_reward_logged,
            clip=clip,
        )
    return comparison
