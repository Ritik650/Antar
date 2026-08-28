"""The specification curve, and the properties that make it honest.

docs/EVALUATION.md §9.5. The curve exists because our own reported figure moved 5.13% ->
4.67% -> 5.83% from analytic choices alone. These tests check that the instrument
measuring that movement is itself trustworthy: that the dimensions move the answer in
the directions they should, that the pre-registered specification is genuinely in the
set, and that the majority rule is applied rather than described.
"""

from __future__ import annotations

import pytest

from antar.eval.specification_curve import (
    ACTIONS,
    MEASUREMENT_WINDOWS,
    NEGATIVE_DEFINITIONS,
    NEGATIVE_THRESHOLD,
    PRE_REGISTERED,
    REFERENCE_INSTANTS,
    SEEDS,
    Specification,
    SpecificationCurve,
    enumerate_specifications,
    evaluate_specification,
    run_specification_curve,
    verdict_line,
)

pytestmark = pytest.mark.statistical

# Small enough to run in the suite; the full-fidelity curve is produced by
# `make evaluate`. Every property below holds at either resolution.
N = 250


@pytest.fixture(scope="module")
def curve() -> SpecificationCurve:
    # The subset must contain the pre-registered specification, or the percentile it
    # is plotted at is undefined - which is exactly what this fixture got wrong first
    # time, and is worth keeping as a comment because the same mistake in the full run
    # would silently drop the headline off its own curve.
    instants = (REFERENCE_INSTANTS[0], PRE_REGISTERED["reference_instant"])
    seeds = (SEEDS[0], SEEDS[1])
    specs = [
        s
        for s in enumerate_specifications("base")
        if s.seed in seeds and s.reference_instant in instants
    ]
    assert any(s.is_pre_registered for s in specs)
    return run_specification_curve("base", n_customers=N, specifications=specs)


def spec(**overrides) -> Specification:
    base = {
        "scenario": "base",
        "reference_instant": PRE_REGISTERED["reference_instant"],
        "seed": PRE_REGISTERED["seed"],
        "measurement_window_days": PRE_REGISTERED["measurement_window_days"],
        "negative_definition": PRE_REGISTERED["negative_definition"],
        "action": PRE_REGISTERED["action"],
    }
    return Specification(**{**base, **overrides})


def test_the_specification_space_is_the_full_cross_product():
    expected = (
        len(REFERENCE_INSTANTS)
        * len(SEEDS)
        * len(MEASUREMENT_WINDOWS)
        * len(NEGATIVE_DEFINITIONS)
        * len(ACTIONS)
    )
    assert len(enumerate_specifications("base")) == expected


def test_exactly_one_specification_is_the_pre_registered_one():
    """The headline must be *in* the distribution it is plotted against, or the
    percentile means nothing."""
    flagged = [s for s in enumerate_specifications("base") if s.is_pre_registered]
    assert len(flagged) == 1


def test_the_pre_registered_specification_matches_the_committed_protocol():
    """Guards against the curve quietly re-defining the headline."""
    assert PRE_REGISTERED["measurement_window_days"] == 30  # EVALUATION.md 4.1
    assert PRE_REGISTERED["negative_definition"] == "strict"  # uplift < 0
    assert PRE_REGISTERED["action"] == "best_available"  # SIMULATOR_CARD 6.3
    assert NEGATIVE_THRESHOLD["strict"] == 0.0


def test_stricter_definitions_of_negative_give_smaller_populations():
    """Monotonicity. A non-monotone result would mean the scan is not doing what it
    says it is doing."""
    shares = [
        evaluate_specification(spec(negative_definition=d), n_customers=N)
        for d in ("strict", "margin", "conservative_ci")
    ]
    assert shares[0] >= shares[1] >= shares[2], shares
    assert (
        NEGATIVE_THRESHOLD["strict"]
        > NEGATIVE_THRESHOLD["margin"]
        > NEGATIVE_THRESHOLD["conservative_ci"]
    )


def test_the_generous_action_gives_the_smaller_population():
    """`best_available` maximises over channels, so fewer customers are left harmed.

    This is the direction that matters for honesty: our pre-registered choice is the
    one *least* favourable to the finding, and this asserts that rather than claiming
    it.
    """
    generous = evaluate_specification(spec(action="best_available"), n_customers=N)
    single_channel = evaluate_specification(spec(action="reference_sms"), n_customers=N)
    assert generous < single_channel, (generous, single_channel)
    assert PRE_REGISTERED["action"] == "best_available"


def test_the_curve_reports_where_the_pre_registered_choice_sits(curve):
    """If the headline sits at the 95th percentile we picked a flattering
    specification, and the reader is entitled to see that."""
    percentile = curve.summary()["pre_registered_percentile"]
    assert percentile is not None
    assert 0.0 <= percentile <= 100.0


def test_the_majority_rule_is_applied_not_merely_described():
    """EVALUATION.md 9.5: fewer than half clearing means the claim is unsupported."""
    pessimistic = SpecificationCurve(scenario="base", threshold=0.05)
    pessimistic.results = [(spec(), 0.01) for _ in range(9)] + [(spec(), 0.9)]
    assert not pessimistic.supported
    assert "unsupported" in verdict_line(pessimistic)

    optimistic = SpecificationCurve(scenario="base", threshold=0.05)
    optimistic.results = [(spec(), 0.2) for _ in range(9)] + [(spec(), 0.01)]
    assert optimistic.supported
    assert "clear the pre-registered" in verdict_line(optimistic)


def test_variance_decomposition_names_every_dimension(curve):
    decomposition = curve.variance_by_dimension()
    assert set(decomposition) == {
        "reference_instant",
        "seed",
        "measurement_window_days",
        "negative_definition",
        "action",
    }
    assert all(v >= 0 for v in decomposition.values())
    # Sorted most-influential first, so the reader sees the choice that mattered.
    values = list(decomposition.values())
    assert values == sorted(values, reverse=True)


def test_specifications_are_deterministic():
    a = evaluate_specification(spec(), n_customers=N)
    b = evaluate_specification(spec(), n_customers=N)
    assert a == b


def test_a_curve_with_no_results_does_not_explode():
    empty = SpecificationCurve(scenario="base", threshold=0.05)
    assert empty.fraction_clearing == 0.0
    assert not empty.supported
    assert empty.summary()["median"] is None
