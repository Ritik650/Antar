"""Compute the phase diagram. `docs/EVALUATION.md` §9.4.

    python -m scripts.make_phase_diagram --customers 400

Two panels over the same grid, because `artifacts/specification_curve_base.json` found
the action set to be the dominant analytic choice:

  * `best_available` - a merchant with every channel integrated
  * `reference_sms`  - a merchant with one SMS integration, which is most merchants

If the indifference boundary moves between them, whether this class of system pays for
itself depends more on the merchant's channel mix than on their customer base.

Runtime is the reason `--customers` defaults low: 11 x 8 x 2 = 176 cells, each a full
generate/detect/allocate cycle.
"""

from __future__ import annotations

import argparse
import time

from antar.config import artifacts_dir, get_config
from antar.detect.pipeline import run_detection
from antar.eval.experiment import run_experiment
from antar.eval.phase_diagram import (
    EXTENDED_OPTOUT_AXIS,
    OPTOUT_AXIS,
    PANELS,
    SELF_HEAL_AXIS,
    Cell,
    PhaseDiagram,
    cell_scenario,
    render_ascii,
    write,
)
from antar.eval.policies import PolicyRunner
from antar.simulator.generator import Generator
from antar.simulator.scenarios import get_scenario


def run_cell(panel: str, self_heal: float, optout: float, *, config, seed: int) -> Cell:
    scenario = cell_scenario(get_scenario("base"), self_heal, optout)

    # `reference_sms` models a merchant with one integration: the action set is
    # restricted to SMS before anything is valued or allocated.
    cell_config = config
    if panel == "reference_sms":
        # Restrict the *action set*, not the cost table. Zeroing other channels' costs
        # left them fully available and produced two identical panels. POSTMORTEM D18.
        cell_config = config.with_overrides({"simulator.available_channels": ["SMS"]})

    batch = Generator(scenario, seed=seed, config=cell_config).generate()
    log = run_experiment(batch, config=cell_config)
    detection = run_detection(batch, config=cell_config)

    from scripts.run_allocation import fit_propensity

    outcomes = PolicyRunner(
        batch,
        log,
        config=cell_config,
        propensity_model=fit_propensity(log),
        detection=detection,
    ).run()

    antar = outcomes["antar"]
    propensity = outcomes["propensity"]
    capacity_price = max(
        (p["price_paise"] for p in antar.shadow_prices if p["binding"]), default=0.0
    )

    return Cell(
        panel=panel,
        mean_self_heal=self_heal,
        mean_optout_sensitivity=optout,
        antar_minus_propensity_per_1000_paise=antar.net_per_1000_paise
        - propensity.net_per_1000_paise,
        antar_net_per_1000_paise=antar.net_per_1000_paise,
        propensity_net_per_1000_paise=propensity.net_per_1000_paise,
        antar_contacts=antar.contacts,
        propensity_contacts=propensity.contacts,
        contact_capacity_shadow_price_paise=capacity_price,
        events=antar.at_risk_events,
    )


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=400)
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    parser.add_argument("--panels", default=",".join(PANELS))
    parser.add_argument(
        "--extend",
        action="store_true",
        help="also sweep the disclosed post-hoc low-opt-out extension (EVALUATION 9.4)",
    )
    args = parser.parse_args()

    config = config.with_overrides(
        {
            "simulator.n_customers": args.customers,
            "simulator.checkout_abandon_events": args.customers // 3,
        }
    )
    panels = [p.strip() for p in args.panels.split(",") if p.strip()]
    optout_values = tuple(OPTOUT_AXIS)
    if args.extend:
        optout_values = tuple(sorted(set(OPTOUT_AXIS) | set(EXTENDED_OPTOUT_AXIS)))
    total = len(panels) * len(SELF_HEAL_AXIS) * len(optout_values)

    diagram = PhaseDiagram()
    started = time.perf_counter()
    done = 0

    for panel in panels:
        for optout in optout_values:
            for self_heal in SELF_HEAL_AXIS:
                diagram.cells.append(
                    run_cell(panel, self_heal, optout, config=config, seed=args.seed)
                )
                done += 1
                if done % 10 == 0 or done == total:
                    elapsed = time.perf_counter() - started
                    rate = elapsed / done
                    print(
                        f"  {done}/{total} cells  {elapsed:.0f}s elapsed  "
                        f"~{rate * (total - done):.0f}s remaining",
                        flush=True,
                    )

    # The indifference band: cells whose advantage is smaller than the spread across
    # the grid's own noise floor are drawn as indifferent rather than assigned a side.
    # Estimated as the median absolute advantage in the cells where the two policies
    # take the same number of contacts, which is where they should agree.
    ties = [
        abs(c.antar_minus_propensity_per_1000_paise)
        for c in diagram.cells
        if c.antar_contacts == c.propensity_contacts
    ]
    diagram.indifference_band_paise = float(sorted(ties)[len(ties) // 2]) if ties else 0.0

    out = write(diagram, artifacts_dir() / "phase_diagram.json")

    print()
    for panel in panels:
        print(render_ascii(diagram, panel))
        print()

    summary = diagram.summary()
    for panel in panels:
        stats = summary["panels"][panel]
        print(
            f"  {panel:16s} Antar wins {stats['antar_wins_share']:.0%} of the grid, "
            f"median advantage Rs {stats['median_delta_per_1000_rupees']:,.0f}/1000, "
            f"capacity binds in {stats['capacity_binds_share']:.0%} of cells"
        )
    print()
    print("  " + summary["interpretation"])
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
