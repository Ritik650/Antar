"""Component retention: does each part of the system earn its place?

Implements the rule pre-registered in `docs/EVALUATION.md` §12.2, and written
**before** `antar/decide/allocator.py` exists so that the ordering is checkable in git.

The rule, restated:

> After M6, re-run the L2 leave-one-out ablation with the full L3 objective. If a
> component's removal does not reduce net incremental rupees, by a margin whose
> bootstrap 95% CI excludes zero, the component is deleted from the system.

The asymmetry is the point. The null hypothesis is that a component does **not**
belong. A CI straddling zero means "not shown to earn its place", and that resolves to
deletion, not to retention. A component kept on an ambiguous interval is a component
kept on its author's affection for it.

Nothing here can be satisfied by an argument. `verdicts()` reads measured numbers or
raises; there is no default that lets a component survive because the measurement was
not run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Components under test. Adding one here commits it to the same rule.
GOVERNED_COMPONENTS: dict[str, str] = {
    "downtime_crosscheck": (
        "The Downtime API cross-check in antar/detect/root_cause.py. Condemned by the "
        "L2-only ablation (LIMITATIONS L7); on trial under the full L3 objective."
    ),
    "changepoint_detector": (
        "The EWMA/CUSUM segment detector in antar/detect/changepoint.py. Condemned by "
        "the L2-only ablation; on trial under the full L3 objective."
    ),
}

DECISION_SCENARIO = "base"
"""A component must justify itself in the reference regime. Winning only in the
scenario built to be easiest is not winning."""


class RetentionNotMeasured(RuntimeError):
    """Raised when a verdict is requested but no measurement exists.

    Deliberately fatal. The failure mode this whole module guards against is a
    component surviving because nobody got round to running the ablation, so "not
    measured" must never silently read as "keep".
    """


@dataclass(frozen=True)
class RetentionVerdict:
    component: str
    keep: bool
    delta_net_paise: int
    """Net incremental rupees WITH the component minus WITHOUT it, in paise.
    Positive means the component earns its place."""
    ci_low_paise: int
    ci_high_paise: int
    scenario: str
    rationale: str

    @property
    def ci_excludes_zero(self) -> bool:
        return self.ci_low_paise > 0 or self.ci_high_paise < 0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ci_excludes_zero": self.ci_excludes_zero}


def decide(
    component: str,
    *,
    delta_net_paise: int,
    ci_low_paise: int,
    ci_high_paise: int,
    scenario: str = DECISION_SCENARIO,
) -> RetentionVerdict:
    """Apply the pre-registered rule. No judgement, no override."""
    positive = delta_net_paise > 0
    excludes_zero = ci_low_paise > 0 or ci_high_paise < 0
    keep = bool(positive and excludes_zero)

    if keep:
        rationale = (
            f"Removing it costs Rs {delta_net_paise / 100:,.0f} in net incremental "
            f"recovery (95% CI Rs {ci_low_paise / 100:,.0f} to "
            f"Rs {ci_high_paise / 100:,.0f}, excludes zero). Kept."
        )
    elif not excludes_zero:
        rationale = (
            f"The effect of removing it has a 95% CI of Rs {ci_low_paise / 100:,.0f} "
            f"to Rs {ci_high_paise / 100:,.0f}, which straddles zero. Not shown to "
            "earn its place. Per EVALUATION.md 12.2 ambiguity resolves to DELETE."
        )
    else:
        rationale = (
            f"Removing it *improves* net incremental recovery by "
            f"Rs {-delta_net_paise / 100:,.0f}. DELETE."
        )

    return RetentionVerdict(
        component=component,
        keep=keep,
        delta_net_paise=delta_net_paise,
        ci_low_paise=ci_low_paise,
        ci_high_paise=ci_high_paise,
        scenario=scenario,
        rationale=rationale,
    )


def artifact_path(root: Path | None = None) -> Path:
    from antar.config import artifacts_dir

    return (root or artifacts_dir()) / "retention.json"


def write_verdicts(verdicts: list[RetentionVerdict], path: Path | None = None) -> Path:
    target = path or artifact_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({v.component: v.as_dict() for v in verdicts}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def read_verdicts(path: Path | None = None) -> dict[str, RetentionVerdict]:
    target = path or artifact_path()
    if not target.exists():
        raise RetentionNotMeasured(
            f"no retention verdict at {target}. Run `python tasks.py evaluate`. "
            "Per docs/EVALUATION.md 12.2 an unmeasured component is not a kept "
            "component - this is deliberately fatal rather than defaulting to keep."
        )
    raw = json.loads(target.read_text(encoding="utf-8"))
    return {
        key: RetentionVerdict(
            component=value["component"],
            keep=bool(value["keep"]),
            delta_net_paise=int(value["delta_net_paise"]),
            ci_low_paise=int(value["ci_low_paise"]),
            ci_high_paise=int(value["ci_high_paise"]),
            scenario=value.get("scenario", DECISION_SCENARIO),
            rationale=value["rationale"],
        )
        for key, value in raw.items()
    }


def enabled(component: str, *, default_when_unmeasured: bool = True) -> bool:
    """Whether a governed component should be wired into the runtime path.

    `default_when_unmeasured=True` only until M6 lands; the retention test asserts it
    is flipped to False once `artifacts/retention.json` is being produced, so the
    permissive default cannot outlive the measurement that replaces it.
    """
    if component not in GOVERNED_COMPONENTS:
        return True
    try:
        return read_verdicts()[component].keep
    except (RetentionNotMeasured, KeyError):
        return default_when_unmeasured
