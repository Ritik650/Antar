"""Is this number a *plausible size*? The question the other two guards do not ask.

`test_results_are_reproducible.py` checks **provenance**: every number in the README
appears in an artifact. `test_artifacts_are_self_consistent.py` checks **internal
consistency**: every derived figure equals its own components.

Both would have passed on a headline that was ten times too large, as long as it was
consistently ten times too large and appeared somewhere. That is the gap D24 actually
opened and neither guard closed: a units error produces a number that is perfectly
traceable, perfectly consistent, and off by a factor of a hundred.

## The instrument: ratios

A ratio of two quantities in the same units **cannot have a units error**. If
incremental recovery is expressed as a fraction of the rupees at risk, no confusion
between paise and rupees, per-cycle and per-thousand, or candidates and events can
survive — the mistake cancels or the fraction leaves (0, 1].

So each check here reduces a headline to a dimensionless quantity and asserts it lies
where arithmetic says it must.

## What these bounds are, and are not

They are **not** calibration targets. Nothing here says "recovery should be about 1%".
They are the boundaries of the physically possible plus a wide margin: you cannot
recover more than the money at risk, an expected probability cannot exceed one, and a
policy cannot contact more customers than it has candidates. A number that violates one
is not disappointing, it is *wrong*, and no amount of provenance makes it right.

Deliberately loose. A tight bound here would fail on a scenario change and be widened
until it meant nothing.
"""

from __future__ import annotations

import json

import pytest

from antar.config import artifacts_dir, get_config

CHECKED: list[str] = []


def load(name: str) -> dict:
    path = artifacts_dir() / name
    if not path.exists():
        pytest.skip(f"{name} not present; run `python tasks.py evaluate`")
    CHECKED.append(name)
    return json.loads(path.read_text(encoding="utf-8"))


def at_risk_rupees_and_events() -> tuple[float, int]:
    """The denominator every ratio below is taken against."""
    data = load("calibration.json")
    scenarios = data if isinstance(data, list) else [data]
    base = next((s for s in scenarios if s.get("scenario") == "base"), scenarios[0])
    summary = base["summary"]
    return summary["at_risk_value_paise"] / 100, summary["at_risk_events"]


# ------------------------------------------------------- recovery


def test_incremental_recovery_is_a_fraction_of_the_money_at_risk():
    """**The check.** You cannot recover more than is at risk.

    Expressed as a fraction, this is immune to every units error the other guards would
    wave through. A result above 1.0 is not a good quarter; it is a defect.
    """
    at_risk, _events = at_risk_rupees_and_events()
    allocation = load("allocation_base.json")

    for row in allocation["policies"]:
        fraction = row["expected_incremental_rupees"] / at_risk
        assert 0.0 < fraction <= 1.0, (
            f"{row['policy']}: expected incremental recovery is "
            f"Rs {row['expected_incremental_rupees']:,.0f} against Rs {at_risk:,.0f} at "
            f"risk - a fraction of {fraction:.2%}. A ratio cannot have a units error, so "
            "this is a real defect in the magnitude, not a labelling problem."
        )


def test_incremental_recovery_is_not_implausibly_close_to_the_whole_book():
    """A separate assertion, because the interesting failure is not `> 1.0`.

    A units error of 100x lands well above 1.0 and the test above catches it. An error
    of 10x might land at 11% and pass. This asserts the fraction is small enough to be
    an *incremental* effect: recovering a third of every rupee at risk by sending
    messages would be an extraordinary claim.
    """
    at_risk, _events = at_risk_rupees_and_events()
    allocation = load("allocation_base.json")

    for row in allocation["policies"]:
        fraction = row["expected_incremental_rupees"] / at_risk
        assert fraction < 0.30, (
            f"{row['policy']} claims to incrementally recover {fraction:.1%} of all "
            "money at risk. That is not an incremental effect, it is a miracle. Check "
            "the denominator before believing it."
        )


# ------------------------------------------------------- harm


def test_the_optout_loss_cannot_exceed_the_book_times_its_multiplier():
    """The harm term has a ceiling too, and it is the one doing the work.

    `expected_optout_loss = p_optout x amount x multiplier`, so as a fraction of the
    money at risk it cannot exceed the multiplier - that would require every customer
    to opt out with probability one.
    """
    at_risk, _events = at_risk_rupees_and_events()
    multiplier = float(get_config().get("decide.optout_loss_multiplier"))
    allocation = load("allocation_base.json")

    for row in allocation["policies"]:
        fraction = row["expected_optout_loss_rupees"] / at_risk
        assert 0.0 <= fraction <= multiplier, (
            f"{row['policy']}: expected opt-out loss is {fraction:.2%} of the money at "
            f"risk, which exceeds the loss multiplier of {multiplier}. That would need "
            "an opt-out probability above 1."
        )


def test_the_implied_optout_probability_is_a_probability():
    """Back out `p_optout` from the reported loss and check it is one.

    This is the check that would have caught a mis-scaled multiplier, a double-counted
    amount, or a per-contact figure reported as a per-candidate one - none of which the
    provenance or consistency guards can see.
    """
    at_risk, events = at_risk_rupees_and_events()
    mean_amount = at_risk / events
    multiplier = float(get_config().get("decide.optout_loss_multiplier"))
    allocation = load("allocation_base.json")

    for row in allocation["policies"]:
        if not row["contacts"]:
            continue
        per_contact = row["expected_optout_loss_rupees"] / row["contacts"]
        implied = per_contact / (mean_amount * multiplier)
        assert 0.0 <= implied <= 1.0, (
            f"{row['policy']}: the reported opt-out loss implies a mean opt-out "
            f"probability of {implied:.3f} per contact, which is not a probability. "
            f"(Rs {per_contact:,.0f} per contact / (mean amount Rs {mean_amount:,.0f} "
            f"x multiplier {multiplier}))"
        )


# ------------------------------------------------------- counting


def test_a_policy_cannot_contact_more_customers_than_it_has_candidates():
    for row in load("allocation_base.json")["policies"]:
        assert 0 <= row["contacts"] <= row["candidates"]
        assert 0 <= row["candidates"] <= row["at_risk_events"]


def test_the_headline_delta_is_bounded_by_the_two_policies_it_compares():
    """The specific field D24 got wrong, bounded from both sides.

    The difference between two nets cannot exceed the range they span. Trivial
    arithmetic, and precisely the arithmetic nothing was doing when the field held the
    changepoint detector's retention effect.
    """
    data = load("allocation_base.json")
    nets = [row["net_per_1000_at_risk_rupees"] for row in data["policies"]]
    delta = data["antar_minus_propensity_per_1000_rupees"]
    assert abs(delta) <= max(nets) - min(nets) + 1.0, (
        f"the headline delta ({delta:,.0f}) is larger than the full spread of the "
        f"policies it compares ({max(nets) - min(nets):,.0f}). It cannot be a difference "
        "between two of them."
    )


def test_the_headline_per_cycle_is_a_sane_multiple_of_the_average_cycle():
    """The arithmetic a Razorpay engineer will do in their head during the panel.

    The headline is a *net* difference, so it legitimately includes avoided harm valued
    at `optout_loss_multiplier` times a cycle - it is not bounded by one cycle's value.
    But it cannot exceed the multiplier times the mean cycle either, because that is the
    most a single avoided cancellation is worth.
    """
    at_risk, events = at_risk_rupees_and_events()
    mean_amount = at_risk / events
    multiplier = float(get_config().get("decide.optout_loss_multiplier"))
    data = load("allocation_base.json")

    per_cycle = data["antar_minus_propensity_per_1000_rupees"] / 1000
    ceiling = mean_amount * (multiplier + 1)
    assert abs(per_cycle) <= ceiling, (
        f"the headline is Rs {per_cycle:,.0f} per at-risk cycle against a mean cycle "
        f"value of Rs {mean_amount:,.0f}. Even valuing every avoided cancellation at the "
        f"full {multiplier}x multiplier, the most one cycle can be worth is "
        f"Rs {ceiling:,.0f}."
    )


# ------------------------------------------------- detection and the batch


def test_every_reported_rate_is_a_rate():
    detection = load("detection_base.json")["detection"]
    assert 0.0 <= detection["accuracy_when_resolved"] <= 1.0
    assert 0.0 <= detection["unknown_rate"] <= 1.0
    for name, row in detection["per_class"].items():
        assert 0.0 <= row["precision"] <= 1.0, name
        assert 0.0 <= row["recall"] <= 1.0, name


def test_the_batch_cannot_recover_more_than_it_had_at_risk():
    at_risk, _events = at_risk_rupees_and_events()
    batch = load("batch_base.json")
    assert 0.0 <= batch["recovered_rupees"] <= at_risk, (
        f"the batch reports recovering Rs {batch['recovered_rupees']:,.0f} against "
        f"Rs {at_risk:,.0f} at risk"
    )


def test_the_specification_curve_reports_shares_not_counts():
    summary = load("specification_curve_base.json")["summary"]
    for key in ("min", "p25", "median", "p75", "max", "fraction_clearing_threshold"):
        assert 0.0 <= summary[key] <= 1.0, f"{key} is {summary[key]}, which is not a share"


# ------------------------------------------------------------- meta


def test_at_least_one_artifact_was_checked():
    assert CHECKED, (
        "no artifact was available, so every magnitude check above skipped. Run "
        "`python tasks.py evaluate` before trusting this file's silence."
    )
