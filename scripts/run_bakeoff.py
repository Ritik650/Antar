"""Run the uplift bake-off. `docs/EVALUATION.md` §6.2.

    python tasks.py bakeoff

Pools the exploration splits of the committed seeds for fitting, because one batch's
exploration split is roughly 540 rows and a sign-recovery F1 computed on ~160 validation
rows containing ~10 negative cases would be measuring noise. The control holdout used
for the inferential claim remains single-seed. Logged as a deviation in §13.

Writes `artifacts/bakeoff_<scenario>.json`. Touches no holdout.
"""

from __future__ import annotations

import argparse
import json
import time

import pandas as pd

from antar.config import artifacts_dir, get_config
from antar.decide.uplift.bakeoff import ranking_table, run_bakeoff
from antar.eval.experiment import ExperimentLog, run_experiment
from antar.simulator.generator import generate


def pooled_log(scenario: str, seeds: list[int], config) -> ExperimentLog:
    logs = [run_experiment(generate(scenario, seed=seed), config=config) for seed in seeds]
    return ExperimentLog(
        features=pd.concat([log.features for log in logs], ignore_index=True),
        frame=pd.concat([log.frame for log in logs], ignore_index=True),
        truth=pd.concat([log.truth for log in logs], ignore_index=True),
        scenario=scenario,
        seed=seeds[0],
    )


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--seeds", default="", help="comma-separated; defaults to eval.seeds")
    parser.add_argument("--timebox", type=float, default=180.0, help="per-learner fit budget")
    args = parser.parse_args()

    seeds = (
        [int(s) for s in args.seeds.split(",")]
        if args.seeds
        else [int(s) for s in config.get("eval.seeds")]
    )

    print(f"building pooled exploration log for {args.scenario} over {len(seeds)} seeds...")
    started = time.perf_counter()
    log = pooled_log(args.scenario, seeds, config)
    print(f"  {len(log)} events, {int(log.exploration.sum())} exploration rows "
          f"({time.perf_counter() - started:.0f}s)")

    result = run_bakeoff(
        log,
        optout_loss_multiplier=float(config.get("decide.optout_loss_multiplier")),
        timebox_seconds=args.timebox,
    )

    out = artifacts_dir() / f"bakeoff_{args.scenario}.json"
    out.write_text(
        json.dumps({**result.as_dict(), "table": ranking_table(result)}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    print(
        f"\ntrain {result.n_train} / validation {result.n_validation}, "
        f"negative prevalence {result.negative_prevalence:.2%}\n"
    )
    header = f"  {'learner':16s} {'AUUC':>9s} {'Qini':>8s} {'prec':>6s} {'rec':>6s} {'F1':>6s} {'bias':>8s} {'abstain Rs':>12s} {'score':>7s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in ranking_table(result):
        marker = " *" if row["selected"] else ("  X" if row["disqualified"] else "  ")
        score = f"{row['score']:+.3f}" if row["score"] == row["score"] else "   -  "
        print(
            f"  {row['learner']:16s} {row['auuc']:>9.4f} {row['qini']:>8.4f} "
            f"{row['sign_precision']:>6.3f} {row['sign_recall']:>6.3f} {row['sign_f1']:>6.3f} "
            f"{row['negative_bias']:>+8.4f} {row['abstention_net_rupees']:>12,.0f} {score:>7s}{marker}"
        )

    if result.disqualified:
        print("\ndisqualified:")
        for name, reason in sorted(result.disqualified.items()):
            print(f"  {name}: {reason}")

    print(f"\nSELECTED: {result.selection_rationale}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
