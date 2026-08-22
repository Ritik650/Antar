"""The retention rule must be unbendable.

`docs/EVALUATION.md` §12.2 commits, before M6 exists, to deleting the Downtime API
cross-check and the EWMA/CUSUM changepoint detector if the post-M6 ablation does not
show them earning their place. These tests exist so that the commitment is enforced by
the build rather than by the author's memory in the last week before a deadline.

Written and committed at the same time as the rule, and before the allocator that will
produce the numbers. That ordering is the evidence that the rule was not written to fit
a result already in hand.
"""

from __future__ import annotations

import json

import pytest

from antar.eval.retention import (
    DECISION_SCENARIO,
    GOVERNED_COMPONENTS,
    RetentionNotMeasured,
    decide,
    read_verdicts,
    write_verdicts,
)

pytestmark = pytest.mark.statistical


def test_a_clear_win_is_kept():
    verdict = decide(
        "downtime_crosscheck", delta_net_paise=500_000, ci_low_paise=120_000,
        ci_high_paise=880_000,
    )
    assert verdict.keep
    assert verdict.ci_excludes_zero


def test_a_clear_loss_is_deleted():
    verdict = decide(
        "changepoint_detector", delta_net_paise=-300_000, ci_low_paise=-540_000,
        ci_high_paise=-60_000,
    )
    assert not verdict.keep
    assert "DELETE" in verdict.rationale


def test_ambiguity_resolves_to_deletion_not_retention():
    """The asymmetry that makes the rule mean anything.

    A CI straddling zero says "not shown to earn its place". Under a keep-by-default
    rule every component survives forever, because a null result is the most common
    outcome of any honest ablation.
    """
    verdict = decide(
        "downtime_crosscheck", delta_net_paise=40_000, ci_low_paise=-210_000,
        ci_high_paise=290_000,
    )
    assert not verdict.keep, "an ambiguous interval must not be enough to keep a component"
    assert "straddles zero" in verdict.rationale


def test_a_positive_point_estimate_alone_is_not_enough():
    """Point estimates are how components survive ablations they should not."""
    assert not decide(
        "changepoint_detector", delta_net_paise=1_000_000, ci_low_paise=-50_000,
        ci_high_paise=2_050_000,
    ).keep


def test_the_decision_scenario_is_the_reference_regime():
    """Winning only in the scenario built to be easiest is not winning."""
    assert DECISION_SCENARIO == "base"


def test_an_unmeasured_component_is_fatal_not_kept(tmp_path):
    """The failure mode this module exists to prevent.

    "We never ran the ablation" must not read as "the component passed".
    """
    with pytest.raises(RetentionNotMeasured):
        read_verdicts(tmp_path / "absent.json")


def test_verdicts_round_trip(tmp_path):
    path = tmp_path / "retention.json"
    written = [
        decide("downtime_crosscheck", delta_net_paise=1, ci_low_paise=-5, ci_high_paise=7),
        decide("changepoint_detector", delta_net_paise=9, ci_low_paise=2, ci_high_paise=15),
    ]
    write_verdicts(written, path)
    loaded = read_verdicts(path)
    assert set(loaded) == {"downtime_crosscheck", "changepoint_detector"}
    assert loaded["changepoint_detector"].keep
    assert not loaded["downtime_crosscheck"].keep


def test_every_condemned_component_is_governed():
    """LIMITATIONS L7 names two components. Both must be under the rule.

    A component condemned by an ablation but absent from `GOVERNED_COMPONENTS` is one
    that quietly escaped the commitment.
    """
    assert set(GOVERNED_COMPONENTS) == {"downtime_crosscheck", "changepoint_detector"}
    for description in GOVERNED_COMPONENTS.values():
        assert len(description) > 60, "each governed component needs a real description"


def test_the_rule_is_committed_in_the_evaluation_protocol():
    """The code and the pre-registration must not drift apart."""
    from pathlib import Path

    protocol = (
        Path(__file__).resolve().parents[2] / "docs" / "EVALUATION.md"
    ).read_text(encoding="utf-8")
    assert "12.2 Component retention rule" in protocol
    # The asymmetry is the load-bearing half of the rule: if this sentence goes, the
    # commitment has quietly become keep-by-default.
    assert 'A CI straddling zero means "not shown to earn its place"' in protocol
    for component in ("Downtime API cross-check", "changepoint detector"):
        assert component in protocol, f"{component} is not named in the pre-registration"


def test_the_permissive_default_cannot_outlive_the_measurement():
    """`enabled()` defaults to True only while no verdict exists.

    Once `artifacts/retention.json` is produced, the verdict governs. This asserts the
    fallback is genuinely conditional rather than an unconditional 'True' wearing a
    try/except.
    """
    from antar.eval import retention

    original = retention.artifact_path
    try:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "retention.json"
            path.write_text(
                json.dumps(
                    {
                        "downtime_crosscheck": decide(
                            "downtime_crosscheck", delta_net_paise=-1, ci_low_paise=-9,
                            ci_high_paise=-1,
                        ).as_dict()
                    }
                ),
                encoding="utf-8",
            )
            retention.artifact_path = lambda root=None: path  # type: ignore[assignment]
            assert retention.enabled("downtime_crosscheck") is False
            # A component with no verdict in a present file still falls back.
            assert retention.enabled("changepoint_detector") is True
            # Anything not governed is always on.
            assert retention.enabled("policy_gate") is True
    finally:
        retention.artifact_path = original  # type: ignore[assignment]
