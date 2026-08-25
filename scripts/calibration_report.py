"""Produce the calibration report for one or all scenarios.

    python tasks.py calibration-report
    python tasks.py calibration-report SCENARIO=conservative

Writes `artifacts/calibration.md` and `artifacts/calibration.json`. Every
`[MEASURE @ M2]` placeholder in docs/SIMULATOR_CARD.md is filled from these.
"""

from __future__ import annotations

import argparse
import json

from antar.config import artifacts_dir, get_config
from antar.simulator.calibration import build_report, render_markdown
from antar.simulator.generator import generate
from antar.simulator.scenarios import REPORTED


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="all", help="'all' or a scenario name")
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    args = parser.parse_args()

    names = list(REPORTED) if args.scenario == "all" else [args.scenario]
    reports = []
    for name in names:
        print(f"generating {name} (seed {args.seed})...", flush=True)
        reports.append(build_report(generate(name, seed=args.seed)))

    out = artifacts_dir()
    (out / "calibration.md").write_text(render_markdown(reports), encoding="utf-8")
    (out / "calibration.json").write_text(
        json.dumps([r.as_dict() for r in reports], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"\nwrote {out / 'calibration.md'}")
    for report in reports:
        status = "clean" if report.taxonomy_clean else f"DIRTY {report.undocumented_reasons}"
        print(
            f"  {report.scenario:13s} events={report.summary['at_risk_events']:5d} "
            f"taxonomy={status} ambiguous={report.ambiguous_share:.1%} "
            f"deliberately-unmapped={report.deliberately_unmapped_share:.1%}"
        )
    return 0 if all(r.taxonomy_clean for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
