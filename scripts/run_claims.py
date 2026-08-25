"""Adjudicate the pre-registered claims and write `artifacts/claims.json`.

    python -m scripts.run_claims

`docs/SIMULATOR_CARD.md` §10 and `antar/eval/claims.py` both state that
`make evaluate` writes this file and that the README refuses to state a withdrawn
claim. CI's determinism step diffs it across two runs.

**It was never written.** `scripts/run_evaluation.py` was built in M8 with seven stages
and the claims registry was not one of them, so a documented output of `make evaluate`
did not exist, and the `.github/workflows/ci.yml` step that diffs it could only ever
fail. Nothing noticed because CI had never run: the repository was pushed for the first
time at the end of M10. POSTMORTEM D29.

The registry is the mechanism that stops a headline outliving its evidence. A claim
whose pre-registered test fails is marked unsupported here, and everything downstream
reads that rather than deciding for itself.
"""

from __future__ import annotations

import argparse

from antar.config import artifacts_dir, get_config
from antar.eval.claims import SCAN_CUSTOMERS, sleeping_dogs_verdict, write_claims


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--customers",
        type=int,
        default=SCAN_CUSTOMERS,
        help="population per scenario for the uplift scan",
    )
    parser.add_argument(
        "--seeds",
        default="",
        help="comma-separated; defaults to eval.seeds truncated to three",
    )
    args = parser.parse_args()

    if args.seeds:
        seeds = tuple(int(s) for s in args.seeds.split(",") if s.strip())
    else:
        # Three seeds, not one: a claim that survives a single draw of the world has
        # survived very little. D13's lesson about pinned reference instants applies
        # here too - `SCAN_REFERENCE` is fixed in claims.py so this is reproducible.
        seeds = tuple(int(s) for s in config.get("eval.seeds"))[:3]

    print(f"adjudicating pre-registered claims over seeds {seeds}...", flush=True)
    verdict = sleeping_dogs_verdict(seeds=seeds, n_customers=args.customers)

    mark = "SUPPORTED" if verdict.supported else "WITHDRAWN"
    print(f"\n  sleeping_dogs: {mark}")
    print(f"  {verdict.detail}")
    for scenario, share in sorted(verdict.evidence["share_by_scenario"].items()):
        margin = verdict.evidence["margin_above_threshold"][scenario]
        print(f"    {scenario:14s} negative share {share:.4f}  (margin {margin:+.4f})")

    out = write_claims([verdict], artifacts_dir() / "claims.json")
    print(f"\nwrote {out}")

    # A withdrawn claim is not a build failure - it is a finding, and the artifact is
    # what carries it. Returning non-zero here would tempt someone to "fix" the claim.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
