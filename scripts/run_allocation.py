"""M6: allocate, price the constraints, and honour the L7 retention verdict.

    python -m scripts.run_allocation --scenario base

Produces:

  * the three-policy comparison under a shared contact capacity
  * LP duals on the capacity rows
  * the **counterfactual price of the TRAI-01 contact window**, obtained by re-solving
    with the window widened to 09:00 - the hour we lost when the primary source
    contradicted PLAN.md - and differencing the objective
  * the retention verdict for the two components `docs/LIMITATIONS.md` L7 condemned,
    now that the full L3 objective exists to judge them against

Framing, per `docs/EVALUATION.md` §7.4: a shadow price on a consumer-protection rule is
the price of a protection. It is a fact about the merchant's operation, not a grievance.
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

from antar.config import artifacts_dir, get_config
from antar.decide.allocator import CounterfactualPrice, summarise_prices
from antar.decide.uplift.learners import BASELINES
from antar.detect.pipeline import run_detection
from antar.eval.experiment import run_experiment
from antar.eval.policies import (
    PolicyRunner,
    antar_minus_propensity_paise,
    comparison_table,
)
from antar.eval.retention import decide, write_verdicts
from antar.simulator.generator import generate


def fit_propensity(log):
    """P2 needs a real model or it is just P1 wearing a different name."""
    exploration = log.split(log.exploration)
    if len(exploration) < 50:
        return None
    model = BASELINES["propensity"]()
    model.fit(
        exploration.features,
        exploration.frame["treated"].to_numpy(),
        exploration.frame["recovered"].to_numpy(),
    )
    return model


def window_price(batch, log, config, *, widened_start_hour: int = 9) -> CounterfactualPrice:
    """What the hour lost to TCCCPR Schedule II Note-1 is worth.

    `TRAI-01` is a feasibility filter, not a capacity row: candidates outside the
    window are removed before the solver sees them, so there is no dual to read. The
    honest price is a counterfactual - re-run the allocation with the window opening at
    09:00 instead of 10:00, and difference the objective.

    Verification against the primary source cost Antar this hour. Quantifying it closes
    the arc.
    """
    detection = run_detection(batch, config=config)
    baseline = PolicyRunner(
        batch, log, config=config, propensity_model=None, detection=detection
    ).run()["antar"]

    widened_config = config.with_overrides(
        {"policy.contact_window": {"start_hour": widened_start_hour, "end_hour": 21}}
    )
    widened = PolicyRunner(
        batch, log, config=widened_config, propensity_model=None, detection=detection
    ).run()["antar"]

    return CounterfactualPrice(
        label=f"TRAI-01 contact window ({widened_start_hour:02d}:00 vs 10:00 opening)",
        baseline_objective_paise=baseline.net_paise,
        relaxed_objective_paise=widened.net_paise,
        events=baseline.events,
        explanation=(
            "TCCCPR Schedule II para 3(1) Note-1 makes the 08:00-10:00 band default-OFF "
            "for every customer, so the permitted window opens at 10:00 rather than the "
            "09:00 PLAN.md assumed from secondary sources. This is the value of that "
            "hour to this merchant. It is the price of a consumer protection, not an "
            "argument against one."
        ),
    )


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    parser.add_argument("--customers", type=int, default=0, help="0 = config default")
    parser.add_argument("--retention-seeds", type=int, default=5)
    args = parser.parse_args()

    if args.customers:
        config = config.with_overrides(
            {
                "simulator.n_customers": args.customers,
                "simulator.checkout_abandon_events": args.customers // 3,
            }
        )

    print(f"generating {args.scenario} (seed {args.seed})...", flush=True)
    started = time.perf_counter()
    batch = generate(args.scenario, seed=args.seed, config=config)
    log = run_experiment(batch, config=config)
    print(f"  {len(log)} events ({time.perf_counter() - started:.0f}s)")

    propensity = fit_propensity(log)
    if propensity is None:
        print("  WARNING: too little exploration data to fit P2; it will mirror P1")

    detection = run_detection(batch, config=config)
    print(f"  detection: {detection.source_mix}, UNKNOWN {detection.unknown_rate:.1%}")
    runner = PolicyRunner(
        batch, log, config=config, propensity_model=propensity, detection=detection
    )
    outcomes = runner.run()
    antar = outcomes["antar"]

    print("\nThree-policy comparison, shared contact capacity, expected value:\n")
    header = f"  {'policy':18s} {'contacts':>9s} {'abstain':>8s} {'incr Rs':>12s} {'optout Rs':>12s} {'net/1000 Rs':>13s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in comparison_table(outcomes):
        print(
            f"  {row['policy']:18s} {row['contacts']:>9d} {row['abstentions']:>8d} "
            f"{row['expected_incremental_rupees']:>12,.0f} "
            f"{row['expected_optout_loss_rupees']:>12,.0f} "
            f"{row['net_per_1000_events_rupees']:>13,.0f}"
        )
    # Named, not `delta`. The retention loop below used to reuse that name, so by the
    # time the artifact was written this headline had been overwritten by the last
    # component's retention effect - a different quantity, of the opposite sign, under
    # a key that said otherwise. POSTMORTEM D24.
    headline_delta_rupees = antar_minus_propensity_paise(outcomes) / 100
    print(
        f"\n  P3 - P2 (the comparison that matters): "
        f"Rs {headline_delta_rupees:,.0f} per 1,000 cycles"
    )

    print("\nShadow prices:")
    for price in antar.shadow_prices:
        status = "BINDING" if price["binding"] else "slack"
        print(
            f"  {price['row_id']:22s} Rs {price['price_rupees']:>10,.2f}  [{status}] "
            f"({price['method']})"
        )
    if all(not p["binding"] for p in antar.shadow_prices):
        print(
            "\n  Every capacity row is SLACK. That is a finding, not a null result: at\n"
            "  this scenario's opt-out sensitivity the binding constraint is not the\n"
            "  merchant's contact capacity but customer tolerance. Antar declines slots\n"
            "  it is entitled to use, so one more slot is worth nothing. See the phase\n"
            "  diagram for where capacity starts to bind."
        )

    print("\nCounterfactual price of the contact window...", flush=True)
    price = window_price(batch, log, config)
    print(
        f"  Rs {price.as_dict()['price_per_1000_events_rupees']:,.2f} per 1,000 at-risk "
        "cycles - the value of the 09:00-10:00 hour that Note-1 removes."
    )

    # ---- L7 retention, now that the full L3 objective exists ----------------
    print("\nComponent retention (docs/EVALUATION.md 12.2)...", flush=True)
    seeds = [int(s) for s in config.get("eval.seeds")][: args.retention_seeds]
    print(f"  measuring across {len(seeds)} seeds (the sampling distribution that applies)")
    verdicts = []
    for component in ("downtime_crosscheck", "changepoint_detector"):
        retention_delta, ci, with_totals, without_totals = retention_across_seeds(
            config, args.scenario, component, seeds
        )
        verdict = decide(
            component,
            delta_net_paise=round(retention_delta),
            ci_low_paise=round(ci[0]),
            ci_high_paise=round(ci[1]),
            scenario=args.scenario,
        )
        verdicts.append(verdict)
        print(f"  {component:22s} {'KEEP' if verdict.keep else 'DELETE'}")
        print(
            f"    with Rs {np.mean(with_totals) / 100:>12,.0f}/1000  "
            f"without Rs {np.mean(without_totals) / 100:>12,.0f}/1000  "
            f"delta Rs {retention_delta / 100:>10,.2f}"
        )
        print(f"    {verdict.rationale}")
    write_verdicts(verdicts)

    out = artifacts_dir() / f"allocation_{args.scenario}.json"
    out.write_text(
        json.dumps(
            {
                "scenario": args.scenario,
                "seed": args.seed,
                "policies": comparison_table(outcomes),
                "antar_minus_propensity_per_1000_rupees": round(headline_delta_rupees, 2),
                "shadow_prices": summarise_prices(
                    _as_allocation(antar), [price]
                ),
                "retention": [v.as_dict() for v in verdicts],
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    return 0


def _as_allocation(outcome):
    """Adapt a PolicyOutcome's recorded prices back into the shape summarise_prices
    expects, so the artifact and the console share one renderer."""
    from antar.decide.allocator import Allocation, ShadowPrice

    allocation = Allocation(objective_paise=outcome.objective_paise, status="Optimal")
    allocation.shadow_prices = [
        ShadowPrice(
            row_id=p["row_id"],
            kind=p["kind"],
            price_paise=p["price_paise"],
            binding=p["binding"],
            method=p["method"],
            explanation=p["explanation"],
            regulation_ids=tuple(p["regulation_ids"]),
        )
        for p in outcome.shadow_prices
    ]
    return allocation


def retention_across_seeds(config, scenario: str, component: str, seeds: list[int]):
    """Net-per-1000 with and without the component, across seeds.

    The value function here is an *expectation* under the ground-truth response model,
    so within a single batch it is deterministic and a within-batch bootstrap would
    report an interval of exactly zero width — which says nothing about uncertainty and
    everything about the estimator being deterministic.

    The real variation is across draws of the world. So the interval is built over
    seeds, which is the sampling distribution that actually applies.
    """
    # Both arms are constructed explicitly. Neither inherits the shipped default, and
    # that is the whole point.
    #
    # The first version built the "with" arm from `config` as-is. That was fine in M6,
    # when both components were still enabled - and became *vacuous* the moment M6's own
    # verdict switched them off, because `with` and `without` then described the same
    # detector and every delta was exactly zero. The measurement would have re-confirmed
    # DELETE forever, on no evidence, and a deleted component could never be
    # reconsidered on new data. POSTMORTEM D25.
    flags = {
        "downtime_crosscheck": "detect.enable_downtime_crosscheck",
        "changepoint_detector": "detect.enable_changepoint_detector",
    }
    extra_off = {
        # Belt and braces: turning the flag off is what removes the component, and these
        # additionally neutralise it in case a future refactor reads the thresholds
        # without consulting the flag.
        "downtime_crosscheck": {"detect.downtime_overlap_tolerance_minutes": -100000},
        "changepoint_detector": {
            "detect.changepoint.degrading_threshold": 1e9,
            "detect.changepoint.degraded_threshold": 1e9,
        },
    }[component]

    enabled = config.with_overrides({flags[component]: True})
    stripped = enabled.with_overrides({flags[component]: False, **extra_off})

    deltas: list[float] = []
    with_totals: list[float] = []
    without_totals: list[float] = []

    for seed in seeds:
        # Shared between arms on purpose: the two arms must differ in the detector
        # and in nothing else, or the delta measures the difference between two
        # worlds rather than between two detectors.
        batch = generate(scenario, seed=seed, config=config)
        log = run_experiment(batch, config=config)
        propensity = fit_propensity(log)

        with_component = PolicyRunner(
            batch, log, config=enabled, propensity_model=propensity,
            detection=run_detection(batch, config=enabled),
        ).run()["antar"]
        without = PolicyRunner(
            batch, log, config=stripped, propensity_model=propensity,
            detection=run_detection(batch, config=stripped),
        ).run()["antar"]

        with_totals.append(with_component.net_per_1000_paise)
        without_totals.append(without.net_per_1000_paise)
        deltas.append(with_component.net_per_1000_paise - without.net_per_1000_paise)

    # A measurement in which the two arms never differ has measured nothing. Reporting
    # it as "delta 0, CI (0, 0), DELETE" would be a verdict wearing the clothes of
    # evidence - and it is exactly what this function produced once the components it
    # judges were switched off (D25). Fail loudly instead.
    if all(w == wo for w, wo in zip(with_totals, without_totals, strict=True)):
        raise RuntimeError(
            f"the retention measurement for {component!r} is vacuous: the enabled and "
            "stripped arms produced identical results on every seed, so the component "
            "had no influence to measure. Either the override no longer disables it, "
            "or the component is already inert. A zero delta from this state is not "
            "evidence for DELETE - it is the absence of evidence. See POSTMORTEM D25."
        )

    array = np.asarray(deltas, dtype=float)
    if array.std() < 1e-9:
        ci = (float(array.mean()), float(array.mean()))
    else:
        rng = np.random.default_rng(seeds[0])
        boots = np.array(
            [rng.choice(array, size=len(array), replace=True).mean() for _ in range(4000)]
        )
        ci = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))

    return float(array.mean()), ci, with_totals, without_totals


if __name__ == "__main__":
    raise SystemExit(main())
