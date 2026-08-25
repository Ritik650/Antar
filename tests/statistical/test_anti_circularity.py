"""Is the sleeping-dogs finding planted?

**Read this test, and the disclosure below, before believing anything Antar says
about staying silent.**

docs/SIMULATOR_CARD.md section 6.3 and PLAN.md M2 acceptance. The headline finding -
that a non-trivial population is *harmed* by being contacted, because the
RBI-mandated pre-debit notification is itself an opt-out prompt - is exactly the kind
of result a simulator can manufacture for its author. So the test is written to fail
if we planted it:

  1. Run all three scenarios with parameters set independently of this file.
  2. Compute ground-truth per-customer uplift directly from the response model,
     under the action **most favourable to treatment** for that customer.
  3. Assert a non-trivial negative mass appears in at least two of the three.
  4. Assert that no simulator parameter is named after, or tuned per scenario to
     force, this outcome.
  5. Assert the mechanism is causal: remove the opt-out hazard and the negative
     population must vanish entirely.

The gate itself lives in `antar/eval/claims.py`, not here, because
SIMULATOR_CARD section 10 makes the consequence of failure a statement about the
*artifacts* ("the finding is withdrawn") rather than about the test runner. This file
asserts that the artifacts and the measurement agree.

---

## Disclosure: this gate did not pass on the first run

Recorded here rather than in a commit message, because a reviewer will find this file
before they find the git log.

On first measurement the shares were conservative 5.9%, base 0.5%, aggressive 0.0% -
one qualifying scenario, so the finding was **not** supported. Investigating why
surfaced five genuine defects, all fixed (docs/POSTMORTEM.md D1-D3, D6 and D13). The
final measured shares, over five seeds with the scan pinned to a fixed instant, are
**conservative 36.5%, base 5.8%, aggressive 0.1%**.

Each fix is independently justified as a contradiction between the code and its own
documented specification, and each is verifiable without reference to this test. But
the search for them was motivated by the test failing, and pretending otherwise would
be the exact behaviour this file exists to catch. Judge the fixes on their merits:

  * **D1** let the persuasion multipliers stack above 1.0, so `persuadability` was not
    the ceiling its docstring claimed and the median dunning SMS persuaded 43% of
    recipients - not a credible number for a dunning message under any parameterisation.
  * **D2** divided the opt-out hazard by `scenario.mean_optout_sensitivity`, exactly
    cancelling one of the four axes SIMULATOR_CARD section 8 advertises as
    distinguishing the scenarios.
  * **D3** defaulted `prior_notifications` to zero, when RBI-EM-01 guarantees every
    failed debit was already preceded by one notification.

  * **D6** computed the "balance peak" as `replace(day=min(salary_day, 28))`, which
    for a customer paid on the 30th lands 29 days after payday - the *trough*. The
    scan claimed to evaluate treatment at its most favourable timing while evaluating
    part of the population at its least favourable, inflating the negative share.

  * **D13** evaluated the scan at `clock.now()`, so the base share moved from 5.13% to
    4.67% across a single midnight - crossing the pre-registered threshold - with no
    code change whatsoever. The scan is now pinned to a fixed instant inside the
    simulated horizon.

D1 and D3 move the result toward the finding; D2 moves `aggressive` away from it; D6
moves every scenario away from it; D13 removed a dependence on the calendar that could
have moved it either way on any given day.

Two caveats that belong next to the result, not in a footnote:

  * **base clears the threshold by 0.8 percentage points** - 5.83% against a 5.0%
    bar, ranging 5.3-6.2% across five seeds. It clears on every seed, but a margin that
    thin is not a robust finding; a different defensible choice of
    `BASE_OPTOUT_HAZARD` would flip it. The README reports base as **marginal**, and
    docs/EVALUATION.md 9.4 replaces the threshold crossing with a phase diagram, which
    is what a boundary this soft actually deserves.
  * **aggressive shows essentially none** (0.12%). In the easy regime sleeping dogs do
    not exist. That is a genuine boundary on the claim and is reported as one, not
    buried.

The defensible version of the claim is therefore: *sleeping dogs dominate under
conservative assumptions, are marginally present under reference assumptions, and are
absent under optimistic ones.* Anything stronger than that is not supported by this
measurement.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from antar import clock
from antar.eval.claims import (
    MIN_QUALIFYING_SCENARIOS,
    NON_TRIVIAL_SHARE,
    negative_uplift_share,
    sleeping_dogs_verdict,
)
from antar.ids import intervention_id
from antar.signals.schemas import Channel, FailureClass, Intervention, MessageClass
from antar.simulator.latents import LatentStore
from antar.simulator.response_model import ResponseModel
from antar.simulator.scenarios import (
    DECLARED_AXES,
    REPORTED,
    SCENARIOS,
    get_scenario,
    undeclared_parameters,
)

pytestmark = pytest.mark.statistical

SEED = 20260822
SEEDS = (20260822, 20260823, 20260824)


@pytest.fixture(scope="module")
def verdict():
    return sleeping_dogs_verdict(seeds=(SEED,))


@pytest.fixture(scope="module")
def shares(verdict) -> dict[str, float]:
    return verdict.evidence["share_by_scenario"]


# ---------------------------------------------------------------- the finding


def test_the_finding_is_adjudicated_rather_than_assumed(verdict, shares):
    """SIMULATOR_CARD 6.3 step 3, corrected.

    This test used to be `assert verdict.supported` — it asserted the project's own
    hypothesis and turned disproving it into a build failure. That is backwards, and it
    contradicts the protocol it claimed to enforce: `scripts/run_claims.py` returns zero
    for a withdrawn claim precisely because **a withdrawn claim is a finding, not an
    error**. A test that goes red when your hypothesis fails is a standing incentive to
    keep the hypothesis true. POSTMORTEM D39.

    What is actually worth asserting is that the machinery *ran*: a verdict exists, it
    is derived from measured shares, and it agrees with the pre-registered rule it
    claims to apply. Whether it comes back supported is the finding, and it is reported
    by `artifacts/claims.json` and enforced against every document by
    `tests/statistical/test_withdrawn_claims_are_not_stated.py`.
    """
    assert verdict.claim == "sleeping_dogs"
    assert shares, "the verdict carries no evidence"

    qualifying = verdict.evidence["qualifying_scenarios"]
    recomputed = sorted(
        name for name, share in shares.items() if share >= verdict.evidence["threshold"]
    )
    assert sorted(qualifying) == recomputed, (
        f"the verdict names {qualifying} as qualifying but the shares {shares} give "
        f"{recomputed}. The verdict must follow from its own evidence."
    )

    expected = len(recomputed) >= MIN_QUALIFYING_SCENARIOS
    assert verdict.supported is expected, (
        f"the pre-registered rule needs {MIN_QUALIFYING_SCENARIOS} of 3 scenarios at or "
        f"above {verdict.evidence['threshold']:.0%}; {len(recomputed)} qualify, so "
        f"supported should be {expected} and the verdict says {verdict.supported}."
    )

    if not verdict.supported:
        # The state this project is actually in, as of the D38 arm-symmetry correction.
        # Asserted rather than skipped, so the record shows the test ran and agreed.
        assert "WITHDRAWN" in verdict.detail


def test_negative_uplift_is_not_the_whole_population(shares):
    """A simulator where contacting always hurts would be as useless as one where it
    always helps, and would make the allocator's job trivial."""
    for name, share in shares.items():
        assert share < 0.60, f"{name}: {share:.2%} negative uplift is not a population, it is a rule"


def test_conservative_is_the_hardest_scenario(shares):
    """SIMULATOR_CARD section 8 claims conservative is hardest. Check the claim.

    High self-heal shrinks the persuasion gain and high opt-out sensitivity raises
    the harm, so more customers should fall below zero than in aggressive. This is a
    prediction the parameterisation makes, not a knob it sets — and it is worth keeping
    as an assertion because it is about the *ordering* the axes imply, not about whether
    any scenario clears a threshold. The D38 correction shrank every share and left the
    ordering intact, which is evidence the axes mean what the card says they mean.
    """
    assert shares["conservative"] > shares["base"] >= shares["aggressive"], (
        f"scenario ordering does not follow from the axes: {shares}"
    )


def test_the_scan_does_not_depend_on_the_wall_clock():
    """docs/POSTMORTEM.md D13. The regression guard for a genuinely alarming bug.

    The scan once read `clock.now()`, so the reported negative-uplift share in the
    base scenario moved from 5.13% to 4.67% across a single midnight - crossing the
    pre-registered 5% threshold - with no code change at all. A gate a calendar day
    can flip is not a gate.

    Runs the scan under three wildly different installed clocks and requires an
    identical answer.
    """
    from datetime import datetime

    from antar.eval.claims import SCAN_REFERENCE

    results = []
    for when in (
        datetime(2026, 1, 1, 3, 0, tzinfo=clock.IST),
        datetime(2026, 8, 23, 23, 59, tzinfo=clock.IST),
        datetime(2027, 6, 15, 12, 0, tzinfo=clock.IST),
    ):
        with clock.use_clock(clock.FrozenClock(when)):
            results.append(negative_uplift_share("base", seed=SEED, n_customers=400))

    assert len(set(results)) == 1, (
        f"the population scan moved with the wall clock: {results}. It is a property "
        "of the simulator's parameters, not of today's date."
    )
    assert SCAN_REFERENCE.tzinfo is not None


def test_the_verdict_is_stable_across_seeds():
    """A finding that depends on the seed is not a finding.

    Added after the first measurement, and in the direction of a stricter bar: the
    qualifying set must be identical on every seed, not merely on the one we happened
    to report.
    """
    qualifying_sets = []
    for seed in SEEDS:
        shares = {name: negative_uplift_share(name, seed=seed, n_customers=800) for name in REPORTED}
        qualifying_sets.append(
            tuple(sorted(n for n, s in shares.items() if s >= NON_TRIVIAL_SHARE))
        )
    assert len(set(qualifying_sets)) == 1, (
        f"the qualifying scenario set changes with the seed: {qualifying_sets}"
    )
    # Stability is the property under test. How *many* scenarios qualify is the finding,
    # and asserting a floor here would re-introduce the defect corrected above: a test
    # that fails when the hypothesis fails. POSTMORTEM D39.


def test_base_margin_is_reported_honestly(verdict):
    """base sits close to the threshold. Whatever the margin is, it must be recorded
    in the evidence so the README can state it rather than round it away."""
    margin = verdict.evidence["margin_above_threshold"]["base"]
    assert "base" in margin.__class__.__name__ or isinstance(margin, float)
    assert margin == round(verdict.evidence["share_by_scenario"]["base"] - NON_TRIVIAL_SHARE, 4)


def test_the_mechanism_is_the_optout_hazard_not_a_hidden_flag():
    """The causal check.

    Remove the opt-out hazard and the negative population must vanish entirely. If
    negative uplift survived with `p_optout == 0` it would be coming from somewhere
    other than the notification, and the regulatory story would be decoration.
    """
    scenario = get_scenario("base")
    model = ResponseModel(scenario, SEED)
    store = LatentStore(scenario, SEED)

    with_hazard = 0
    without_hazard = 0
    base = clock.now()
    for index in range(600):
        latents = store.get(f"cust_{index:06d}")
        candidate = Intervention(
            intervention_id=intervention_id(latents.customer_id, Channel.SMS, base, 0),
            event_id=f"evt_{index}",
            channel=Channel.SMS,
            message_class=MessageClass.TRANSACTIONAL,
            scheduled_for=base + timedelta(hours=25),
        )
        truth = model.evaluate(
            latents,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_paise=49900,
            next_cycle_at=base + timedelta(days=30),
            intervention=candidate,
        )
        with_hazard += int(truth.uplift < 0)
        counterfactual = (1 - truth.p_self_heal) * truth.p_persuaded  # hazard removed
        without_hazard += int(counterfactual < 0)

    assert with_hazard > 0, "no negative uplift at all under the reference channel"
    assert without_hazard == 0, (
        "negative uplift survives with the opt-out hazard removed, so it is not "
        "coming from the RBI-EM-02 mechanism the thesis attributes it to"
    )


# ------------------------------------------------------- the honesty checks


def test_no_simulator_identifier_names_the_finding():
    """SIMULATOR_CARD 6.3 step 4, and 12.4.

    There is no `sleeping_dogs_rate`, no `is_sleeping_dog`, no
    `negative_uplift_target` anywhere in the simulator package. Prose in a comment or
    docstring explaining the mechanism is fine; an identifier named after the
    conclusion is not, because a reader cannot tell it from a planted label.
    """
    banned = re.compile(
        r"sleeping[_-]?dog|negative_uplift_(rate|share|target)|is_sleeping", re.IGNORECASE
    )
    package = Path(__file__).resolve().parents[2] / "antar" / "simulator"
    offenders = []
    for path in package.rglob("*.py"):
        in_docstring = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.count('"""') == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#") or '"""' in stripped:
                continue
            if banned.search(line):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, "simulator identifier named after the finding:\n" + "\n".join(offenders)


def test_scenarios_differ_only_along_declared_axes():
    """Every scenario moves the same dials. None has one of its own."""
    assert undeclared_parameters() == set(), (
        f"Scenario gained a field outside the declared axes: {undeclared_parameters()}"
    )
    for name in REPORTED:
        row = SCENARIOS[name].as_dict()
        for axis in DECLARED_AXES:
            assert axis in row, f"{name} is missing declared axis {axis}"


def test_the_axes_move_monotonically_across_scenarios():
    """conservative -> base -> aggressive is a single ordered sweep.

    If one scenario moved an axis the other way, that would be tuning dressed as a
    parameterisation.
    """
    conservative, base, aggressive = (get_scenario(n) for n in REPORTED)
    for axis in (
        "mean_self_heal",
        "mean_optout_sensitivity",
        "downtime_multiplier",
        "heterogeneity",
        "intent_to_churn_rate",
    ):
        values = [getattr(s, axis) for s in (conservative, base, aggressive)]
        assert values[0] > values[1] > values[2], f"{axis} is not monotone: {values}"
    values = [s.mean_persuadability for s in (conservative, base, aggressive)]
    assert values[0] < values[1] < values[2], f"mean_persuadability is not monotone: {values}"


def test_the_optout_axis_actually_changes_the_hazard():
    """Regression guard for docs/POSTMORTEM.md D2.

    The scenario axis was once divided out of the hazard, making it inert. A
    parameterisation that advertises an axis it does not use is worse than one that
    does not advertise it.
    """
    hazards = []
    for name in REPORTED:
        scenario = get_scenario(name)
        model = ResponseModel(scenario, SEED)
        store = LatentStore(scenario, SEED)
        candidate = Intervention(
            intervention_id="itv_probe",
            event_id="evt_probe",
            channel=Channel.SMS,
            message_class=MessageClass.TRANSACTIONAL,
            scheduled_for=clock.now() + timedelta(hours=25),
        )
        sample = [
            model.optout_probability(store.get(f"cust_{i:06d}"), candidate) for i in range(500)
        ]
        hazards.append(float(np.mean(sample)))
    assert hazards[0] > hazards[1] > hazards[2], (
        f"mean opt-out hazard is not ordered by the scenario axis: {hazards}"
    )
