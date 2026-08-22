"""No inferential result may depend on when it was computed.

The suite-level gate that generalises docs/POSTMORTEM.md D13, where the pre-registered
anti-circularity threshold flipped across a midnight because the population scan read
`clock.now()`.

`tests/unit/test_clock.py` already forbids `datetime.now()` outside `antar/clock.py`.
The D13 code obeyed that rule — it called `clock.now()`. The rule named a *mechanism*
where it should have named a *property*, so this file names the property:

> No statistical claim's value may depend on the wall-clock instant at which it is
> computed.

Two halves, because a registry alone rots:

  1. Every registered inferential entry point is run under three clocks eighteen months
     apart and must return an identical value.
  2. Anything in `antar/eval/` that could produce a number must be **either** registered
     **or** explicitly exempted with a written reason. A new inferential function cannot
     quietly escape the gate by not being added to a list.

See docs/CLOCK_AUDIT.md for the classification of every clock read in the package.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from antar import clock

pytestmark = pytest.mark.statistical

SEED = 20260822

# Deliberately far apart, and deliberately including instants that fall outside the
# simulated horizon, before it, and after it. D13 moved on a one-day shift; a gate that
# only tried adjacent days would be testing less than it appears to.
PROBE_CLOCKS: tuple[datetime, ...] = (
    datetime(2026, 1, 1, 3, 0, tzinfo=clock.IST),
    datetime(2026, 8, 23, 23, 59, tzinfo=clock.IST),
    datetime(2027, 6, 15, 12, 0, tzinfo=clock.IST),
)


def _negative_uplift_share() -> float:
    from antar.eval.claims import negative_uplift_share

    return negative_uplift_share("base", seed=SEED, n_customers=300)


def _sleeping_dogs_verdict() -> Any:
    from antar.eval.claims import sleeping_dogs_verdict

    verdict = sleeping_dogs_verdict(seeds=(SEED,), n_customers=300)
    return (verdict.supported, tuple(sorted(verdict.evidence["share_by_scenario"].items())))


def _holdout_assignment() -> Any:
    from antar.eval.holdout import Holdout

    holdout = Holdout(control_share=0.20, salt="antar-2026-08-22")
    return tuple(holdout.arm_of(f"cust_{i:06d}").value for i in range(200))


def _specification_evaluation() -> float:
    from antar.eval.specification_curve import PRE_REGISTERED, Specification, evaluate_specification

    return evaluate_specification(
        Specification(scenario="base", **PRE_REGISTERED), n_customers=200
    )


def _experiment_log() -> Any:
    """A whole batch, end to end, on a deliberately small population.

    Registered rather than exempted because this is the pipeline every reported number
    flows through. Every timestamp in it should derive from `event.occurred_at`, not
    from the clock - and "should" is what a gate is for.
    """
    from antar.config import load_config
    from antar.eval.experiment import run_experiment
    from antar.simulator.generator import generate

    config = load_config(environ={}).with_overrides(
        {"simulator.n_customers": 120, "simulator.checkout_abandon_events": 40}
    )
    batch = generate("base", seed=SEED, config=config)
    log = run_experiment(batch, config=config)
    return (
        len(log),
        int(log.frame["treated"].sum()),
        int(log.frame["recovered"].sum()),
        round(float(log.frame["propensity"].sum()), 6),
    )


def _retention_decision() -> Any:
    from antar.eval.retention import decide

    verdict = decide(
        "downtime_crosscheck", delta_net_paise=1200, ci_low_paise=-400, ci_high_paise=2800
    )
    return (verdict.keep, verdict.delta_net_paise, verdict.rationale)


# Every inferential entry point. Adding a function here is how a new reported number
# comes under the gate; `test_no_unregistered_inferential_function` is what makes
# forgetting to add it fail.
INFERENTIAL_ENTRY_POINTS: dict[str, Callable[[], Any]] = {
    "claims.negative_uplift_share": _negative_uplift_share,
    "claims.sleeping_dogs_verdict": _sleeping_dogs_verdict,
    "holdout.arm_assignment": _holdout_assignment,
    "retention.decide": _retention_decision,
    "specification_curve.evaluate_specification": _specification_evaluation,
    "experiment.run_experiment": _experiment_log,
}

# Functions in `antar/eval/` that are not inferential, each with a reason. An empty
# reason is not accepted.
EXEMPT: dict[str, str] = {
    "experiment.run_experiment_runner": (
        "Placeholder key retained so the mapping shape is obvious; the runner itself "
        "is exercised through the registered `experiment.run_experiment`."
    ),
    "claims.best_available_uplift": (
        "A helper called by negative_uplift_share, which is registered. Covered "
        "transitively; registering both would double the runtime for no extra coverage."
    ),
    "claims.write_claims": (
        "Serialises an already-computed verdict to disk. The inference happened in "
        "sleeping_dogs_verdict, which is registered."
    ),
    "claims.read_claims": (
        "Deserialises a verdict from disk. Returns whatever was written; performs no "
        "computation of its own."
    ),
    "retention.artifact_path": (
        "Returns a filesystem path. No computation, no clock read, nothing reportable."
    ),
    "retention.write_verdicts": (
        "Serialises already-computed verdicts. The decision was made in `decide`, "
        "which is registered."
    ),
    "retention.read_verdicts": (
        "Deserialises verdicts from disk, or raises if none exist. Performs no "
        "computation of its own."
    ),
    "retention.enabled": (
        "Reads a stored verdict and returns a boolean; the inference happened in "
        "`decide`, which is registered."
    ),
    "holdout.stratum_of": (
        "Pure string construction from an event's own fields. Reads no clock and "
        "produces a label, not a number."
    ),
    "specification_curve.run_specification_curve": (
        "A loop over `evaluate_specification`, which is registered. Varying the "
        "reference instant is the entire point of this function, so pinning it would "
        "defeat the artifact."
    ),
    "specification_curve.enumerate_specifications": (
        "Builds the cross product of the specification space. Pure enumeration of "
        "committed constants; reads no clock and computes no estimate."
    ),
    "specification_curve.verdict_line": (
        "Formats an already-computed curve into the sentence the README uses. No "
        "computation beyond the summary statistics the curve already holds."
    ),
    "specification_curve.write_curve": (
        "Serialises an already-computed curve to disk. The inference happened in "
        "`evaluate_specification`, which is registered."
    ),
}


@pytest.mark.parametrize("name", sorted(INFERENTIAL_ENTRY_POINTS))
def test_inferential_results_are_identical_under_every_clock(name):
    """The gate. Run it three times, eighteen months apart, get the same answer."""
    fn = INFERENTIAL_ENTRY_POINTS[name]
    results = []
    for instant in PROBE_CLOCKS:
        with clock.use_clock(clock.FrozenClock(instant)):
            results.append(fn())

    first = results[0]
    for instant, result in zip(PROBE_CLOCKS[1:], results[1:], strict=True):
        assert result == first, (
            f"{name} returned a different value at {instant.isoformat()} than at "
            f"{PROBE_CLOCKS[0].isoformat()}.\n  {PROBE_CLOCKS[0].date()}: {first}\n"
            f"  {instant.date()}: {result}\n"
            "An inferential result may not depend on when it was computed. See "
            "docs/CLOCK_AUDIT.md and POSTMORTEM D13."
        )


def test_inferential_results_are_identical_under_the_real_clock():
    """The system clock is the case that actually bit us: nobody installs a fake one
    in production, and `make evaluate` runs on whatever day it runs."""
    for name, fn in sorted(INFERENTIAL_ENTRY_POINTS.items()):
        with clock.use_clock(clock.SystemClock()):
            live = fn()
        with clock.use_clock(clock.FrozenClock(PROBE_CLOCKS[0])):
            pinned = fn()
        assert live == pinned, f"{name} differs between the system clock and a fixed one"


def test_no_unregistered_inferential_function():
    """A registry nobody updates is a registry that stops covering the code.

    Every public function in `antar/eval/` must be registered above or exempted with a
    written reason. Adding a reported number without doing either fails here.
    """
    import importlib
    import pkgutil

    import antar.eval as eval_pkg

    unclassified: list[str] = []
    for info in pkgutil.walk_packages(eval_pkg.__path__, prefix="antar.eval."):
        module = importlib.import_module(info.name)
        short = info.name.rsplit(".", 1)[-1]
        for fn_name, obj in vars(module).items():
            if fn_name.startswith("_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", None) != info.name:
                continue
            key = f"{short}.{fn_name}"
            if key not in INFERENTIAL_ENTRY_POINTS and key not in EXEMPT:
                unclassified.append(key)

    assert not unclassified, (
        "unclassified function(s) in antar/eval/:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nAdd each to INFERENTIAL_ENTRY_POINTS (so it is checked for time "
        "invariance) or to EXEMPT with a written reason."
    )


def test_every_exemption_carries_a_real_reason():
    """An exemption list is how this gate gets defeated. Make it cost something."""
    for key, reason in EXEMPT.items():
        assert len(reason) > 40, f"{key} needs a real reason, not a label"


def test_the_gate_would_catch_a_clock_dependent_result():
    """A guard that cannot fail is not a guard."""

    def leaky() -> int:
        return clock.now().day

    results = []
    for instant in PROBE_CLOCKS:
        with clock.use_clock(clock.FrozenClock(instant)):
            results.append(leaky())
    assert len(set(results)) > 1, "the probe clocks are too similar to detect drift"


def test_determinism_is_seed_and_instant_not_seed_alone():
    """docs/CLOCK_AUDIT.md, corrected definition.

    `test_determinism.py` checked seed-invariance and passed for the entire life of
    D13. Determinism is *both* halves, and this asserts the second explicitly so the
    definition cannot quietly revert.
    """
    from antar.eval.claims import negative_uplift_share

    with clock.use_clock(clock.FrozenClock(PROBE_CLOCKS[0])):
        a = negative_uplift_share("base", seed=SEED, n_customers=200)
    with clock.use_clock(clock.FrozenClock(PROBE_CLOCKS[2])):
        b = negative_uplift_share("base", seed=SEED, n_customers=200)
    with clock.use_clock(clock.FrozenClock(PROBE_CLOCKS[2])):
        c = negative_uplift_share("base", seed=SEED + 1, n_customers=200)

    assert a == b, "same seed, different instant: results must match"
    assert a != c, "different seed must actually change the result, or this proves nothing"
