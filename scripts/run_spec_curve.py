"""The specification curve: the same question under every defensible analytic choice.

    python -m scripts.run_spec_curve --scenario base

Until M8 this artifact was produced by an ad-hoc call from a notebook cell, which meant
`make evaluate` could not reproduce it and N4 was quietly false for one number. It now
has a script like every other stage.

The output is a distribution, not a point. A headline that survives one specification
out of forty is not a finding; a headline that survives thirty-eight is.
"""

from __future__ import annotations

import argparse
import time

from antar.config import artifacts_dir
from antar.eval.specification_curve import (
    enumerate_specifications,
    run_specification_curve,
    verdict_line,
    write_curve,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--customers", type=int, default=600)
    parser.add_argument("--limit", type=int, default=0, help="0 = every specification")
    args = parser.parse_args()

    specs = enumerate_specifications(args.scenario)
    if args.limit:
        # Deterministic truncation for a quick run. Recorded in the artifact via the
        # specification count, so a partial curve cannot be mistaken for a full one.
        specs = specs[: args.limit]

    print(f"evaluating {len(specs)} specifications on {args.scenario} "
          f"({args.customers} customers each)...", flush=True)
    started = time.perf_counter()
    curve = run_specification_curve(
        args.scenario, n_customers=args.customers, specifications=specs
    )
    print(f"  {time.perf_counter() - started:.0f}s")

    summary = curve.summary()
    percentile = summary.get("pre_registered_percentile")
    print(f"\n  median: {summary.get('median')}")
    print(f"  share clearing the bar: {summary.get('fraction_clearing_threshold')}")
    # `share_positive` is not a key this summary produces, so the old line printed
    # "None" every run and nobody read it. Report the percentile honestly instead:
    # a truncated run legitimately excludes the pre-registered specification, and
    # "not in this run's set" is information where "None" is noise. POSTMORTEM D35.
    print(
        "  pre-registered specification percentile: "
        + (f"{percentile}" if percentile is not None else "not in this run's set")
    )
    print(f"\n  {verdict_line(curve)}")

    if summary.get("variance_by_dimension"):
        print("\n  Variance explained by analytic choice:")
        for dimension, share in sorted(
            summary["variance_by_dimension"].items(), key=lambda kv: -kv[1]
        ):
            print(f"    {dimension:28s} {share}")

    out = write_curve(curve, artifacts_dir() / f"specification_curve_{args.scenario}.json")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
