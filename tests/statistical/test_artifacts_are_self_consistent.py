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
import os

import pytest

from antar.config import artifacts_dir

REQUIRE_ARTIFACTS = os.environ.get("ANTAR_REQUIRE_ARTIFACTS") == "1"
"""Whether a missing artifact is a failure or a skip.

Both readings are right in different places, and conflating them is what turned this
file red on a clean checkout (POSTMORTEM D28).

  * **A fresh clone has no artifacts, and that is correct.** `python tasks.py evaluate`
    is what produces them, and it takes tens of minutes. Failing here would mean a
    contributor cannot run the suite without first running the evaluation, and would
    make CI's lint-and-test job depend on a forty-minute job it does not need.
  * **After the evaluation has run, a skip is a lie.** That is the vacuum this file was
    written to prevent: a suite that passes because it checked nothing.

So the distinction is set by the caller. CI's `reproducibility` job runs the evaluation
and then re-runs these tests with `ANTAR_REQUIRE_ARTIFACTS=1`, which is the only place
the artifacts are known to exist.
"""

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

    antar = by_policy["antar"]["net_per_1000_at_risk_rupees"]
    propensity = by_policy["propensity"]["net_per_1000_at_risk_rupees"]
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
    expected_sign = by_policy["antar"]["net_per_1000_at_risk_rupees"] > by_policy[
        "propensity"
    ]["net_per_1000_at_risk_rupees"]
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
        assert row["contacts"] + row["abstentions"] == row["candidates"], (
            f"{row['policy']}: {row['contacts']} + {row['abstentions']} != "
            f"{row['candidates']}. A candidate that is neither contacted nor abstained "
            "on has gone missing."
        )
        assert row["at_risk_events"] >= row["candidates"], (
            "there cannot be more candidates than at-risk events: every candidate is "
            "an event that survived the feasibility filter"
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
    """Guards against the whole file passing by skipping.

    Only meaningful once the evaluation has run - see `REQUIRE_ARTIFACTS`.
    """
    if not REQUIRE_ARTIFACTS:
        if CHECKED:
            return
        pytest.skip(
            "no artifacts present. This is normal on a fresh checkout; run "
            "`python tasks.py evaluate`, or set ANTAR_REQUIRE_ARTIFACTS=1 to make "
            "their absence a failure."
        )
    assert CHECKED, (
        "ANTAR_REQUIRE_ARTIFACTS=1 was set - the evaluation is supposed to have run - "
        "but every check in this file skipped for want of an artifact. Either the "
        "evaluation did not write what it claims to, or the loader is looking in the "
        "wrong place. A suite that passes by checking nothing is worse than no suite."
    )


def test_the_headline_decomposes_into_recovery_and_harm():
    """The decomposition `RESULTS.md` prints must add up to the headline it decomposes.

    Without this the two could drift and a reader would be told the headline is 99%
    harm-avoidance on the strength of arithmetic nothing checks. D27 is the reason the
    decomposition is published at all.
    """
    data = load("allocation_base.json")
    by_policy = {row["policy"]: row for row in data["policies"]}
    antar, ranker = by_policy["antar"], by_policy["propensity"]

    recovery_gap = (
        antar["incremental_per_1000_at_risk_rupees"]
        - ranker["incremental_per_1000_at_risk_rupees"]
    )
    harm_gap = (
        ranker["optout_loss_per_1000_at_risk_rupees"]
        - antar["optout_loss_per_1000_at_risk_rupees"]
    )
    cost_gap = (
        ranker["channel_cost_rupees"] - antar["channel_cost_rupees"]
    ) * 1000 / antar["at_risk_events"]

    assert recovery_gap + harm_gap + cost_gap == pytest.approx(
        data["antar_minus_propensity_per_1000_rupees"], abs=1.0
    ), (
        "recovery difference + harm difference + channel-cost difference must equal the "
        "headline. If it does not, the decomposition published in RESULTS.md is telling "
        "a reader something the numbers do not support."
    )


def test_the_harm_term_dominates_the_headline_and_is_disclosed():
    """A guard on the *story*, not just the arithmetic.

    If a future change made the headline genuinely recovery-driven, this fails and the
    prose in the README and L18 has to be revisited - which is the point. Right now the
    prose says 99.4% harm avoidance, and that claim needs to keep being true.
    """
    data = load("allocation_base.json")
    by_policy = {row["policy"]: row for row in data["policies"]}
    harm_gap = (
        by_policy["propensity"]["optout_loss_per_1000_at_risk_rupees"]
        - by_policy["antar"]["optout_loss_per_1000_at_risk_rupees"]
    )
    share = harm_gap / data["antar_minus_propensity_per_1000_rupees"]

    # The bound was 0.90 when the harm share was 99.4%. After D28 and D32 it is 86.5%,
    # and this test failing is what forced the README, L18 and the video script to be
    # rewritten rather than left saying "overwhelmingly". The bound is now the claim the
    # docs actually make: harm dominates, but recovery is a material minority.
    assert 0.5 < share < 0.95, (
        f"avoided harm is {share:.1%} of the headline. The docs describe it as the "
        "dominant term with recovery a material minority. Outside this band that "
        "description is wrong - update the prose, do not widen the bound."
    )


def test_the_deployed_model_is_the_one_the_rule_selected():
    """`pipeline.UPLIFT_MODEL` must equal the bake-off's selection.

    The pre-registered rule in `docs/EVALUATION.md` 6.2 determines which learner ships.
    A hardcoded constant that drifts from it means the trace says one model and the
    selection procedure says another - and the whole pre-registration argument rests on
    those being the same thing.

    This caught a real divergence: the constant stayed `x_learner` after the D28/D32
    corrections changed the data and the rule switched to `r_learner`.
    """
    from antar.pipeline import UPLIFT_MODEL

    selected = load("bakeoff_base.json")["selected"]
    assert selected == UPLIFT_MODEL, (
        f"pipeline.UPLIFT_MODEL is {UPLIFT_MODEL!r} but the pre-registered selection "
        f"rule chose {selected!r}. Either ship what the rule selected, or record an ADR "
        "explaining why the rule is being overridden - but do not let them disagree "
        "silently."
    )


def test_the_razorpay_roundtrip_shows_what_it_claims():
    """The live-integration evidence, checked rather than trusted.

    `docs/LIMITATIONS.md` L19 and the README both assert that a replayed idempotency key
    returned the same order. That is the property `PolicyGate` depends on to make a retry
    a replay rather than a second charge, and it is the one claim in the round-trip
    artifact that a truncated id cannot support on its own.
    """
    data = load("razorpay_roundtrip.json")

    assert data["mode"] == "test", "the artifact must come from a test-mode account"
    assert data["key_id_prefix"].startswith("rzp_test_")
    assert data["succeeded"] >= 1

    by_call = {r["call"]: r for r in data["records"]}

    idempotency = by_call.get("idempotency: replay returned the same order")
    assert idempotency is not None, (
        "the artifact does not record the idempotency comparison, so the README's claim "
        "about it rests on nothing"
    )
    assert idempotency["ok"], (
        "a replayed idempotency key did NOT return the same order. PolicyGate treats a "
        "retried action as a replay on the strength of this; if it is false, a retry can "
        "charge twice."
    )

    # The deliberate 400 is the evidence that the error envelope was really observed.
    failure = next((r for r in data["records"] if "unknown" in r["call"]), None)
    assert failure is not None and not failure["ok"]
    for field in ("code", "source", "step", "reason"):
        assert failure.get(field), (
            f"the captured error envelope has no {field!r}. "
            "antar/signals/razorpay_errors.py parses that field, and this artifact is "
            "what shows the name is real rather than inferred from documentation."
        )


def test_the_roundtrip_artifact_carries_no_credentials():
    """It is committed, so this is a security control and not a tidiness one."""
    import json as _json
    import re

    from antar.config import artifacts_dir

    path = artifacts_dir() / "razorpay_roundtrip.json"
    if not path.exists():
        pytest.skip("no roundtrip artifact")
    raw = path.read_text(encoding="utf-8")

    # A full test key is 20+ characters after the prefix; the artifact stores 3.
    assert not re.search(r"rzp_test_[A-Za-z0-9]{8,}", raw), "a full key id is in the artifact"
    assert "key_secret" not in raw.lower()
    for record in _json.loads(raw)["records"]:
        recorded = record.get("id")
        if recorded:
            assert recorded.endswith("..."), f"{recorded} is not truncated"
