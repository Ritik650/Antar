"""The specification curve: how much did our own analytic choices move the answer?

Implements `docs/EVALUATION.md` §9.5, pre-registered before it was run.

## Why

The base-scenario negative-uplift share has been reported, during this build, as 5.13%,
then 4.67%, then 5.83%. **None of that came from changing the simulator.** It came from
a reference instant (POSTMORTEM D13), a peak-finding bug (D6), and a seed protocol —
roughly 1.2 percentage points of movement against a 5.0-point threshold, from analytic
choices alone.

Disclosing each move individually is not enough. A reader is entitled to ask "what if
you had chosen differently?" about every decision, and the only honest answer is to
enumerate the choices and run all of them.

The output answers three questions a point estimate cannot:

  * **Is the base result a finding or a coin flip?** The fraction of specifications
    clearing the threshold.
  * **Which of our choices mattered?** A variance decomposition across dimensions, so a
    reader can see whether the wobble is seed noise (unavoidable) or the definition of
    "negative" (a judgement call we should own).
  * **Did we, consciously or not, pick a flattering specification?** The pre-registered
    specification is marked on the curve. If it sits at the optimistic end, that is
    visible rather than hidden.

## The rule that makes it binding

Pre-registered in §9.5: **if fewer than half of specifications clear the 5% bar, the
base-scenario claim is reported as unsupported**, whatever the pre-registered
specification says. A result that survives only its own analytic choices is not a
result.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, pstdev
from typing import Any

import numpy as np

from antar import clock
from antar.eval.claims import (
    MIN_QUALIFYING_SCENARIOS,
    NON_TRIVIAL_SHARE,
    SCAN_AMOUNT_PAISE,
)
from antar.ids import intervention_id
from antar.signals.schemas import (
    CONTACT_CHANNELS,
    Channel,
    FailureClass,
    Intervention,
    MessageClass,
)
from antar.simulator.latents import LatentStore
from antar.simulator.response_model import ResponseModel
from antar.simulator.scenarios import get_scenario

# ---------------------------------------------------------------------------
# The specification space. Every dimension is a choice we made or could have made.
# Fixed before the curve was computed; see EVALUATION.md 9.5.
# ---------------------------------------------------------------------------

REFERENCE_INSTANTS: tuple[datetime, ...] = (
    datetime(2026, 4, 3, 11, 4, tzinfo=clock.IST),
    datetime(2026, 4, 12, 11, 4, tzinfo=clock.IST),
    datetime(2026, 4, 21, 11, 4, tzinfo=clock.IST),
    datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST),  # the pre-registered one
    datetime(2026, 5, 9, 11, 4, tzinfo=clock.IST),
    datetime(2026, 5, 20, 11, 4, tzinfo=clock.IST),
)

SEEDS: tuple[int, ...] = (20260822, 20260823, 20260824, 20260825, 20260826)

MEASUREMENT_WINDOWS: tuple[int, ...] = (15, 30, 60)

NEGATIVE_DEFINITIONS: tuple[str, ...] = ("strict", "margin", "conservative_ci")
"""How "negative uplift" is operationalised.

  * `strict`          - uplift < 0. The pre-registered definition.
  * `margin`          - uplift < -0.005. Ignores a hair's-breadth negative as noise.
  * `conservative_ci` - uplift < -0.02. Only counts customers clearly harmed.

These are progressively stricter, so the share should fall monotonically across them.
That monotonicity is itself checked, because a non-monotone result would mean the
scan is doing something other than what it claims.
"""

ACTIONS: tuple[str, ...] = ("best_available", "reference_sms")
"""Which action uplift is evaluated under.

`best_available` takes the maximum over channels - generous to treatment, and the
pre-registered choice. `reference_sms` uses a single channel, which is closer to what a
real merchant with one SMS integration would do.
"""

PRE_REGISTERED = {
    "reference_instant": REFERENCE_INSTANTS[3],
    "seed": SEEDS[0],
    "measurement_window_days": 30,
    "negative_definition": "strict",
    "action": "best_available",
}

NEGATIVE_THRESHOLD = {"strict": 0.0, "margin": -0.005, "conservative_ci": -0.02}


@dataclass(frozen=True)
class Specification:
    scenario: str
    reference_instant: datetime
    seed: int
    measurement_window_days: int
    negative_definition: str
    action: str

    @property
    def is_pre_registered(self) -> bool:
        return (
            self.reference_instant == PRE_REGISTERED["reference_instant"]
            and self.seed == PRE_REGISTERED["seed"]
            and self.measurement_window_days == PRE_REGISTERED["measurement_window_days"]
            and self.negative_definition == PRE_REGISTERED["negative_definition"]
            and self.action == PRE_REGISTERED["action"]
        )

    def key(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "reference_instant": self.reference_instant.isoformat(),
            "seed": self.seed,
            "measurement_window_days": self.measurement_window_days,
            "negative_definition": self.negative_definition,
            "action": self.action,
        }


@dataclass
class SpecificationCurve:
    scenario: str
    threshold: float
    results: list[tuple[Specification, float]] = field(default_factory=list)

    @property
    def shares(self) -> list[float]:
        return [share for _spec, share in self.results]

    @property
    def pre_registered_share(self) -> float | None:
        for spec, share in self.results:
            if spec.is_pre_registered:
                return share
        return None

    @property
    def fraction_clearing(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for s in self.shares if s >= self.threshold) / len(self.shares)

    @property
    def supported(self) -> bool:
        """EVALUATION.md 9.5: a majority of specifications must clear the bar."""
        return self.fraction_clearing >= 0.5

    def percentile(self, q: float) -> float:
        return float(np.percentile(self.shares, q)) if self.shares else 0.0

    def variance_by_dimension(self) -> dict[str, float]:
        """How much of the spread each dimension accounts for.

        For each dimension, the standard deviation of the per-level mean share. A
        dimension whose levels all give the same answer contributes nothing; one whose
        levels disagree is a choice the reader should know we made.
        """
        dimensions = (
            "reference_instant",
            "seed",
            "measurement_window_days",
            "negative_definition",
            "action",
        )
        out: dict[str, float] = {}
        for dimension in dimensions:
            grouped: dict[Any, list[float]] = {}
            for spec, share in self.results:
                grouped.setdefault(getattr(spec, dimension), []).append(share)
            level_means = [float(np.mean(v)) for v in grouped.values()]
            out[dimension] = float(pstdev(level_means)) if len(level_means) > 1 else 0.0
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "threshold": self.threshold,
            "n_specifications": len(self.results),
            "pre_registered_share": self.pre_registered_share,
            "median": round(median(self.shares), 5) if self.shares else None,
            "p25": round(self.percentile(25), 5),
            "p75": round(self.percentile(75), 5),
            "min": round(min(self.shares), 5) if self.shares else None,
            "max": round(max(self.shares), 5) if self.shares else None,
            "fraction_clearing_threshold": round(self.fraction_clearing, 4),
            "supported": self.supported,
            "pre_registered_percentile": self._pre_registered_percentile(),
            "variance_by_dimension": {
                k: round(v, 5) for k, v in self.variance_by_dimension().items()
            },
        }

    def _pre_registered_percentile(self) -> float | None:
        """Where the pre-registered choice sits in its own distribution.

        If it sits at the 95th percentile, we chose - however unintentionally - a
        flattering specification, and a reader should be told.
        """
        value = self.pre_registered_share
        if value is None or not self.shares:
            return None
        below = sum(1 for s in self.shares if s < value)
        return round(100.0 * below / len(self.shares), 1)

    def rows(self) -> list[dict[str, Any]]:
        return [
            {**spec.key(), "share": round(share, 5), "pre_registered": spec.is_pre_registered}
            for spec, share in sorted(self.results, key=lambda r: r[1])
        ]


def _uplift_under(
    model: ResponseModel,
    latents: Any,
    *,
    reference: datetime,
    window_days: int,
    action: str,
) -> float:
    """Ground-truth uplift for one customer under one specification."""
    if action == "best_available":
        # The same computation as `claims.best_available_uplift`, but evaluated at this
        # specification's reference instant instead of the pinned one - varying that
        # instant is precisely what the curve exists to measure.
        return _best_available_at(model, latents, reference=reference, window_days=window_days)

    peak = latents.next_balance_peak(reference + timedelta(hours=24))
    candidate = Intervention(
        intervention_id=intervention_id(latents.customer_id, Channel.SMS, peak, 0),
        event_id=f"spec_{latents.customer_id}",
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        scheduled_for=peak,
    )
    truth = model.evaluate(
        latents,
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        amount_paise=SCAN_AMOUNT_PAISE,
        next_cycle_at=reference + timedelta(days=window_days),
        intervention=candidate,
    )
    return truth.uplift


def _best_available_at(
    model: ResponseModel, latents: Any, *, reference: datetime, window_days: int
) -> float:
    peak = latents.next_balance_peak(reference + timedelta(hours=24))
    best = -1.0
    for channel in sorted(CONTACT_CHANNELS, key=lambda c: c.value):
        candidate = Intervention(
            intervention_id=intervention_id(latents.customer_id, channel, peak, 0),
            event_id=f"spec_{latents.customer_id}",
            channel=channel,
            message_class=MessageClass.TRANSACTIONAL,
            scheduled_for=peak,
        )
        truth = model.evaluate(
            latents,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_paise=SCAN_AMOUNT_PAISE,
            next_cycle_at=reference + timedelta(days=window_days),
            intervention=candidate,
        )
        best = max(best, truth.uplift)
    return best


def evaluate_specification(spec: Specification, *, n_customers: int = 600) -> float:
    """The negative-uplift share under one specification."""
    scenario = get_scenario(spec.scenario)
    model = ResponseModel(scenario, spec.seed)
    store = LatentStore(scenario, spec.seed)
    threshold = NEGATIVE_THRESHOLD[spec.negative_definition]

    uplifts = np.array(
        [
            _uplift_under(
                model,
                store.get(f"cust_{index:06d}"),
                reference=spec.reference_instant,
                window_days=spec.measurement_window_days,
                action=spec.action,
            )
            for index in range(n_customers)
        ]
    )
    return float((uplifts < threshold).mean())


def enumerate_specifications(scenario: str) -> list[Specification]:
    return [
        Specification(
            scenario=scenario,
            reference_instant=instant,
            seed=seed,
            measurement_window_days=window,
            negative_definition=definition,
            action=action,
        )
        for instant, seed, window, definition, action in itertools.product(
            REFERENCE_INSTANTS, SEEDS, MEASUREMENT_WINDOWS, NEGATIVE_DEFINITIONS, ACTIONS
        )
    ]


def run_specification_curve(
    scenario: str = "base",
    *,
    n_customers: int = 600,
    threshold: float = NON_TRIVIAL_SHARE,
    specifications: Sequence[Specification] | None = None,
) -> SpecificationCurve:
    specs = list(specifications) if specifications is not None else enumerate_specifications(scenario)
    curve = SpecificationCurve(scenario=scenario, threshold=threshold)
    for spec in specs:
        curve.results.append((spec, evaluate_specification(spec, n_customers=n_customers)))
    return curve


def verdict_line(curve: SpecificationCurve) -> str:
    """The sentence the README uses. Generated, never typed."""
    summary = curve.summary()
    if curve.supported:
        # The percentile only exists if the pre-registered specification is *in* the
        # evaluated set. A truncated run - `--limit`, as CI's QUICK mode uses - can
        # legitimately exclude it, and an earlier version rendered that as "the Noneth
        # percentile" straight into RESULTS.md. A generated sentence that cannot say
        # "unknown" will say something false instead. POSTMORTEM D35.
        percentile = summary["pre_registered_percentile"]
        placement = (
            f"The pre-registered specification sits at the {percentile}th percentile "
            "of that distribution."
            if percentile is not None
            else (
                "The pre-registered specification was not among the specifications "
                "evaluated on this run, so its percentile is not reported. That "
                "happens on a truncated run and means this line is weaker evidence "
                "than the full curve."
            )
        )
        return (
            f"Across {summary['n_specifications']} analytic specifications, the "
            f"{curve.scenario} negative-uplift share has a median of "
            f"{summary['median']:.2%} (IQR {summary['p25']:.2%}-{summary['p75']:.2%}), "
            f"and {summary['fraction_clearing_threshold']:.0%} of specifications clear "
            f"the pre-registered {curve.threshold:.0%} bar. " + placement
        )
    return (
        f"Across {summary['n_specifications']} analytic specifications, only "
        f"{summary['fraction_clearing_threshold']:.0%} clear the pre-registered "
        f"{curve.threshold:.0%} bar (median {summary['median']:.2%}). Per "
        "docs/EVALUATION.md 9.5 the base-scenario claim is reported as **unsupported**: "
        "a result that survives only its own analytic choices is not a result."
    )


def write_curve(curve: SpecificationCurve, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "summary": curve.summary(),
                "verdict": verdict_line(curve),
                "pre_registered": {
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in PRE_REGISTERED.items()
                },
                "min_qualifying_scenarios": MIN_QUALIFYING_SCENARIOS,
                "specifications": curve.rows(),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "Specification",
    "SpecificationCurve",
    "enumerate_specifications",
    "evaluate_specification",
    "run_specification_curve",
    "verdict_line",
    "write_curve",
]
