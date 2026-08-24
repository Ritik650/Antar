"""Every derived number in an artifact must be recomputable from that artifact.

`artifacts/allocation_base.json` carried a key named
`antar_minus_propensity_per_1000_rupees` whose value was **the changepoint detector's
retention effect** — a different quantity, of the opposite sign, produced because a
local variable called `delta` was reused between computing the headline and writing it
out. POSTMORTEM D24.

Nothing caught it for two milestones. The number was plausible, the key was right, the
units were right, and no test compared it to the components it was supposedly derived
from. So this file does that, generally: a derived figure that cannot be reconstructed
from its own artifact's parts is either wrong or undocumented, and both are worth
failing on.

These tests **skip** when the artifact is absent rather than failing, because
`python tasks.py evaluate` is what produces them and a fresh clone has not run it yet.
`test_at_least_one_artifact_was_checked` stops that skip from turning the whole file
into a no-op that nobody notices.
"""

from __future__ import annotations

import json

import pytest

from antar.config import artifacts_dir

CHECKED: list[str] = []


def load(name: str) -> dict:
    path = artifacts_dir() / name
    if not path.exists():
        pytest.skip(f"{name} not present; run `python tasks.py evaluate`")
    CHECKED.append(name)
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------- allocation


def test_the_headline_delta_is_the_difference_it_names():
    """The exact defect D24 describes, stated as arithmetic."""
    data = load("allocation_base.json")
    by_policy = {row["policy"]: row for row in data["policies"]}

    antar = by_policy["antar"]["net_per_1000_events_rupees"]
    propensity = by_policy["propensity"]["net_per_1000_events_rupees"]
    recorded = data["antar_minus_propensity_per_1000_rupees"]

    assert recorded == pytest.approx(antar - propensity, abs=1.0), (
        f"the artifact says the Antar-minus-propensity delta is {recorded}, but its own "
        f"policy table gives {antar} - {propensity} = {antar - propensity}. One of the "
        "two is wrong, and a headline number that disagrees with the table printed "
        "above it is the worst possible way to find out. See POSTMORTEM D24."
    )


def test_the_headline_delta_has_the_sign_the_table_implies():
    """A separate assertion, because a sign error is the failure a reader actually
    notices and the one that most damages a claim."""
    data = load("allocation_base.json")
    by_policy = {row["policy"]: row for row in data["policies"]}
    expected_sign = by_policy["antar"]["net_per_1000_events_rupees"] > by_policy[
        "propensity"
    ]["net_per_1000_events_rupees"]
    recorded_sign = data["antar_minus_propensity_per_1000_rupees"] > 0
    assert recorded_sign == expected_sign


def test_each_policys_net_is_its_own_components():
    """net = incremental - channel cost - opt-out loss, for every policy, every time."""
    data = load("allocation_base.json")
    for row in data["policies"]:
        derived = (
            row["expected_incremental_rupees"]
            - row["channel_cost_rupees"]
            - row["expected_optout_loss_rupees"]
        )
        assert row["net_rupees"] == pytest.approx(derived, abs=1.0), (
            f"{row['policy']}: net {row['net_rupees']} does not equal its parts "
            f"({derived})"
        )


def test_contacts_and_abstentions_account_for_every_event():
    data = load("allocation_base.json")
    for row in data["policies"]:
        assert row["contacts"] + row["abstentions"] == row["events"], (
            f"{row['policy']}: {row['contacts']} + {row['abstentions']} != {row['events']}. "
            "An event that is neither contacted nor abstained on has gone missing."
        )


def test_a_retention_verdict_matches_its_own_confidence_interval():
    """The pre-registered rule: ambiguity resolves to DELETE. A verdict that disagrees
    with the interval printed beside it would make the rule decorative."""
    data = load("allocation_base.json")
    for row in data.get("retention", []):
        excludes_zero = row["ci_low_paise"] > 0 or row["ci_high_paise"] < 0
        assert row["ci_excludes_zero"] == excludes_zero, (
            f"{row['component']}: ci_excludes_zero={row['ci_excludes_zero']} but the "
            f"interval is ({row['ci_low_paise']}, {row['ci_high_paise']})"
        )
        if not excludes_zero:
            assert not row["keep"], (
                f"{row['component']} was kept on an interval straddling zero, which is "
                "the opposite of what EVALUATION.md 12.2 pre-registered"
            )


# ------------------------------------------------------------- detection


def test_detection_support_sums_to_the_events_evaluated():
    data = load("detection_base.json")["detection"]
    support = sum(row["support"] for row in data["per_class"].values())
    assert support == data["n_events"], (
        f"per-class support sums to {support} but the report says {data['n_events']} "
        "events were evaluated"
    )


def test_the_wrong_action_cost_total_is_the_sum_of_its_parts():
    data = load("detection_base.json")["detection"]
    parts = sum(row["wrong_action_cost_rupees"] for row in data["per_class"].values())
    assert data["total_wrong_action_cost_rupees"] == pytest.approx(parts, abs=1.0)


# --------------------------------------------------------------- batch


def test_the_batch_accounts_for_every_event():
    data = load("batch_base.json")
    assert data["contacted"] + data["abstained"] + data["refused"] == data["events"], (
        "contacted + abstained + refused must be every event. A batch that loses "
        "events silently loses money silently."
    )
    assert data["decided"] == data["events"]


def test_the_batch_ledger_has_an_entry_for_every_stage_it_claims():
    """Four entries per event (event, diagnosis, decision, outcome) plus one action per
    approved contact. An entry count that does not reconcile means either a write was
    dropped or the summary is counting something else."""
    data = load("batch_base.json")
    expected = 4 * data["events"] + data["contacted"] + data["refused"]
    assert data["ledger_entries"] == expected, (
        f"ledger has {data['ledger_entries']} entries; the summary implies {expected}"
    )


def test_the_batch_records_that_it_was_simulated():
    """N6, enforced on the artifact rather than trusted to the prose around it."""
    assert load("batch_base.json")["simulated"] is True


def test_the_batch_chain_verified():
    data = load("batch_base.json")
    assert data["chain_verified"] is True
    assert data["replay_self_consistent"] is True


# ---------------------------------------------------- specification curve


def test_the_curve_percentiles_are_ordered():
    summary = load("specification_curve_base.json")["summary"]
    assert summary["min"] <= summary["p25"] <= summary["median"] <= summary["p75"] <= summary["max"]


def test_the_curve_reports_as_many_results_as_it_claims():
    data = load("specification_curve_base.json")
    assert len(data["specifications"]) == data["summary"]["n_specifications"]


# ----------------------------------------------------------- the meta-test


def test_at_least_one_artifact_was_checked():
    """Without this, a missing `artifacts/` directory turns the whole file green by
    skipping, and a suite that passes by not running is worse than no suite."""
    assert CHECKED, (
        "no artifact was available to check. Run `python tasks.py evaluate` before "
        "trusting this file's silence."
    )
