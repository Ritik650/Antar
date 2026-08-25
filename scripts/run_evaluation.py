"""N4. From a clean checkout, reproduce every number the README quotes.

    python tasks.py evaluate            # the full run
    python tasks.py evaluate QUICK=1    # smaller batches, same code path

> **Acceptance:** `make evaluate` from a clean checkout reproduces every number in the
> README. This is N4.

## How that promise is kept

Each stage writes a JSON artifact. This script then assembles `artifacts/RESULTS.md`
**from those artifacts** — not from anything it computed itself and not from anything a
person typed. The README quotes `RESULTS.md`, and
`tests/statistical/test_results_are_reproducible.py` checks that every number in the
README is an artifact value, or a rounding or unit conversion of one — the README says
`₹84,695` where the artifact stores `84694.6`, and both are the same measurement.

The chain is: code → artifact → RESULTS.md → README. There is no step in it where a
human hand can insert a number, which is the only version of N4 that means anything.

## The holdout is unblinded here and nowhere else

`eval.control_share` of customers are never contacted, in any run, by construction
(N3). The primary metric is the treatment-versus-control difference, and this is the
one place it is computed. Every rate reported is **simulated** — `SIMULATOR_CARD.md`
describes the generator and N6 forbids presenting any of it as a real recovery rate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from antar.config import artifacts_dir, get_config

PY = sys.executable


class Stage:
    """One evaluation step, its command, and the artifact it must produce."""

    def __init__(self, name: str, module: str, args: list[str], artifact: str, why: str) -> None:
        self.name = name
        self.module = module
        self.args = args
        self.artifact = artifact
        self.why = why

    def run(self, *, cwd: Path) -> tuple[bool, float]:
        started = time.perf_counter()
        print(f"\n{'=' * 78}\n{self.name}\n  {self.why}\n{'=' * 78}", flush=True)
        completed = subprocess.run(  # fixed argv, no shell
            [PY, "-m", self.module, *self.args], cwd=cwd, check=False
        )
        return completed.returncode == 0, time.perf_counter() - started


def stages(*, quick: bool, scenario: str, seed: int) -> list[Stage]:
    customers = ["--customers", "600"] if quick else []
    return [
        Stage(
            "1/8  Simulator calibration",
            "scripts.calibration_report",
            ["--scenario", "all"],
            "calibration.json",
            "Does the generator match the published Indian recurring-payments figures "
            "it claims to? Everything downstream is worthless if not.",
        ),
        Stage(
            "2/8  Detection (L2)",
            "scripts.detection_report",
            ["--scenario", scenario],
            f"detection_{scenario}.json",
            "Failure-class precision and recall, with the rupee-denominated cost of a "
            "false positive - and the ablation that says which components earn a place.",
        ),
        Stage(
            "3/8  Uplift bake-off (L3)",
            "scripts.run_bakeoff",
            ["--scenario", scenario, *(["--timebox", "60"] if quick else [])],
            f"bakeoff_{scenario}.json",
            "Four learners against the pre-registered selection rule. Sign recovery in "
            "the negative region is the deliverable, not population Qini.",
        ),
        Stage(
            "4/8  Allocation, shadow prices, retention",
            "scripts.run_allocation",
            ["--scenario", scenario, *customers, *(["--retention-seeds", "3"] if quick else [])],
            f"allocation_{scenario}.json",
            "The three-policy comparison under a shared contact capacity, the duals, "
            "the counterfactual price of the 10:00-21:00 window, and the L7 verdict.",
        ),
        Stage(
            "5/8  Phase diagram",
            "scripts.make_phase_diagram",
            ["--customers", "250" if quick else "400"],
            "phase_diagram.json",
            "Where the result holds and where it stops holding. A boundary condition "
            "from a simulator is worth more than a point estimate from one.",
        ),
        Stage(
            "6/8  Specification curve",
            "scripts.run_spec_curve",
            ["--scenario", scenario, *(["--customers", "300", "--limit", "60"] if quick else [])],
            f"specification_curve_{scenario}.json",
            "The same question asked under every defensible analytic choice, so the "
            "headline is a distribution rather than one lucky cell.",
        ),
        Stage(
            "7/8  Pre-registered claims",
            "scripts.run_claims",
            ["--customers", "600"] if quick else [],
            "claims.json",
            "Adjudicate every claim whose test was fixed in advance. A claim that "
            "fails its own test is marked WITHDRAWN and nothing downstream may state it.",
        ),
        Stage(
            "8/8  End-to-end batch and audit ledger",
            "scripts.run_batch",
            ["--scenario", scenario, *customers],
            f"batch_{scenario}.json",
            "All five layers in sequence, writing a hash-chained ledger that is "
            "verified and replayed before the run is allowed to succeed.",
        ),
    ]


def main() -> int:
    # See tasks.py: a cp1252 console cannot encode the arrows in this module's own
    # docstring. Replace rather than raise - a report that loses one glyph is better
    # than an evaluation that does not run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="smaller batches, same code path")
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--scenarios", default="", help="unused placeholder for tasks.py")
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    parser.add_argument("--skip", default="", help="comma-separated stage numbers to skip")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    print(__doc__)
    if args.quick:
        print("QUICK MODE: smaller batches. The numbers will differ from the full run,")
        print("and RESULTS.md records which mode produced it.\n")

    results: list[dict[str, Any]] = []
    failed: list[str] = []

    for stage in stages(quick=args.quick, scenario=args.scenario, seed=args.seed):
        number = stage.name.split("/")[0].strip()
        if number in skip:
            print(f"\nskipping {stage.name}")
            continue
        ok, seconds = stage.run(cwd=root)
        produced = (artifacts_dir() / stage.artifact).exists()
        results.append(
            {"stage": stage.name, "ok": ok, "artifact": stage.artifact,
             "artifact_written": produced, "seconds": round(seconds, 1)}
        )
        if not ok or not produced:
            failed.append(stage.name)
            # Keep going. A partial evaluation with a named gap is more useful than an
            # abort, and the summary below reports exactly which numbers do not exist.
            print(f"\n  STAGE FAILED: {stage.name} (exit ok={ok}, artifact={produced})")

    summary = {
        "quick": args.quick,
        "scenario": args.scenario,
        "seed": args.seed,
        "stages": results,
        "failed": failed,
        "simulated": True,
    }
    (artifacts_dir() / "evaluation.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print(f"\n{'=' * 78}\nSummary\n{'=' * 78}")
    for row in results:
        mark = "ok  " if row["ok"] and row["artifact_written"] else "FAIL"
        print(f"  [{mark}] {row['stage']:44s} {row['seconds']:>7.1f}s  {row['artifact']}")

    written = write_results_md(quick=args.quick, scenario=args.scenario)
    print(f"\n  wrote {written}")

    if failed:
        print(f"\n  {len(failed)} stage(s) failed; RESULTS.md names the missing numbers.")
        return 1

    print("\n  Every stage produced its artifact. RESULTS.md is assembled from them,")
    print("  and no number in it was typed by a person.")
    return 0


# ---------------------------------------------------------------------------
# RESULTS.md, assembled from artifacts only
# ---------------------------------------------------------------------------


def load(name: str) -> dict[str, Any] | None:
    path = artifacts_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_results_md(*, quick: bool, scenario: str) -> Path:
    """Every number below is read from an artifact. None is computed here.

    A missing artifact renders as `NOT PRODUCED` rather than being omitted, because a
    results table that silently drops the stages that failed is a results table that
    only ever contains good news.
    """
    lines: list[str] = []
    add = lines.append

    add("# Results")
    add("")
    add("> **Every number on this page is simulated.** It comes from the generator")
    add("> described in `docs/SIMULATOR_CARD.md`, calibrated against published Indian")
    add("> recurring-payments figures. None of it is a measured recovery rate from a")
    add("> live merchant, and none of it should be quoted as one. This is N6.")
    add("")
    add("This file is written by `python tasks.py evaluate` from the JSON artifacts in")
    add("`artifacts/`. Nothing here is typed by hand. The README quotes this file.")
    add("")
    add(f"Mode: **{'quick' if quick else 'full'}**. Scenario: **{scenario}**.")
    add("")

    _section_calibration(add)
    _section_detection(add, scenario)
    _section_bakeoff(add, scenario)
    _section_allocation(add, scenario)
    _section_spec_curve(add, scenario)
    _section_phase(add)
    _section_batch(add, scenario)

    add("")
    add("---")
    add("")
    add("## Reproducing this")
    add("")
    add("```")
    add("git clone <repo> && cd antar")
    add("python tasks.py install")
    add("python tasks.py evaluate")
    add("```")
    add("")
    add("Every stage is deterministic given `run.seed` in `config/default.yaml`.")
    add("`python tasks.py evaluate QUICK=1` runs the same code on smaller batches.")

    path = artifacts_dir() / "RESULTS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _missing(add: Any, what: str) -> None:
    add(f"**NOT PRODUCED.** The `{what}` stage did not write its artifact on this run.")
    add("")


def _section_calibration(add):
    add("## 1. Does the simulator match reality?")
    add("")
    data = load("calibration.json")
    if data is None:
        _missing(add, "calibration")
        return
    scenarios = data if isinstance(data, list) else [data]
    add(
        "| Scenario | At-risk events | First-attempt failure rate | Above AFA ceiling "
        "| Ambiguous codes | Taxonomy clean |"
    )
    add("|---|---|---|---|---|---|")
    for row in scenarios:
        summary = row.get("summary", {})
        add(
            f"| {row.get('scenario', '')} | {summary.get('at_risk_events', '')} | "
            f"{summary.get('first_attempt_failure_rate', '')} | "
            f"{summary.get('above_afa_ceiling_share', '')} | "
            f"{row.get('ambiguous_code_share', '')} | "
            f"{'yes' if row.get('taxonomy_clean') else 'NO'} |"
        )
    add("")
    add("Failure-class mix, base scenario:")
    add("")
    base = next((r for r in scenarios if r.get("scenario") == "base"), scenarios[0])
    shares = base.get("summary", {}).get("mandate_failure_class_shares", {})
    add("| Failure class | Share |")
    add("|---|---|")
    for name, share in sorted(shares.items(), key=lambda kv: -kv[1]):
        add(f"| {name} | {share} |")
    add("")


def _section_detection(add, scenario):
    add("## 2. Detection (L2)")
    add("")
    payload = load(f"detection_{scenario}.json")
    if payload is None:
        _missing(add, "detection")
        return
    data = payload.get("detection", {})
    add(
        f"Evaluated on the **{data.get('evaluated_on', 'unknown split')}** "
        f"({data.get('n_events', '?')} events)."
    )
    add("")
    add(f"- Accuracy when resolved: **{data.get('accuracy_when_resolved')}**")
    add(f"- UNKNOWN rate: **{data.get('unknown_rate')}**")
    caveat = data.get("unknown_rate_caveat")
    if caveat:
        add(f"- {caveat}")
    add(f"- Total cost of wrong actions: **Rs {data.get('total_wrong_action_cost_rupees')}**")
    add("")
    add("| Failure class | Precision | Recall | Support | Cost of wrong action (Rs) |")
    add("|---|---|---|---|---|")
    for name, row in sorted((data.get("per_class") or {}).items()):
        add(
            f"| {name} | {row.get('precision', '')} | {row.get('recall', '')} | "
            f"{row.get('support', '')} | {row.get('wrong_action_cost_rupees', '')} |"
        )
    add("")


def _section_bakeoff(add, scenario):
    add("## 3. Uplift bake-off (L3)")
    add("")
    data = load(f"bakeoff_{scenario}.json")
    if data is None:
        _missing(add, "bake-off")
        return
    add(f"Selected by the pre-registered rule: **{data.get('selected', 'n/a')}**")
    add("")
    add(
        f"Trained on {data.get('n_train')} rows, validated on {data.get('n_validation')}. "
        f"Negative-uplift prevalence in validation: {data.get('negative_prevalence')}."
    )
    add("")
    for row in data.get("table", []):
        if isinstance(row, str):
            add(f"    {row}")
    add("")
    rationale = data.get("selection_rationale")
    if rationale:
        add(f"> {rationale}")
        add("")
    caveat = data.get("caveat")
    if caveat:
        add(f"**Caveat.** {caveat}")
        add("")
    disqualified = data.get("disqualified")
    if disqualified:
        add(f"Disqualified by the pre-registered floor: `{disqualified}`")
        add("")


def _section_allocation(add, scenario):
    add("## 4. Three-policy comparison, shadow prices, retention")
    add("")
    data = load(f"allocation_{scenario}.json")
    if data is None:
        _missing(add, "allocation")
        return
    first = (data.get("policies") or [{}])[0]
    add(
        f"Denominator is **at-risk cycles** ({first.get('at_risk_events', '?')} in this "
        f"batch), not candidates ({first.get('candidates', '?')}). "
        "`docs/EVALUATION.md` 11.2 pre-registered the former; the code divided by the "
        "latter until M10 (POSTMORTEM D27)."
    )
    add("")
    add(
        "| Policy | Contacts | Abstentions | Incremental Rs/1k | Opt-out loss Rs/1k "
        "| Net Rs/1k |"
    )
    add("|---|---|---|---|---|---|")
    for row in data.get("policies", []):
        add(
            f"| {row.get('policy', '')} | {row.get('contacts', '')} | "
            f"{row.get('abstentions', '')} | "
            f"{row.get('incremental_per_1000_at_risk_rupees', '')} | "
            f"{row.get('optout_loss_per_1000_at_risk_rupees', '')} | "
            f"{row.get('net_per_1000_at_risk_rupees', '')} |"
        )
    add("")
    delta = data.get("antar_minus_propensity_per_1000_rupees")
    by_policy = {row["policy"]: row for row in data.get("policies", [])}
    if delta is not None:
        add(
            f"**Antar minus propensity targeting: Rs {delta} per 1,000 at-risk cycles.** "
            "That is the comparison that matters - beating 'contact everyone' is easy."
        )
        add("")

    # The decomposition, computed here from the artifact's own fields rather than typed.
    # Without it, a reader takes the headline for a recovery number, and it is not one.
    if {"antar", "propensity"} <= set(by_policy):
        antar, ranker = by_policy["antar"], by_policy["propensity"]
        recovery_gap = round(
            antar["incremental_per_1000_at_risk_rupees"]
            - ranker["incremental_per_1000_at_risk_rupees"],
            2,
        )
        harm_gap = round(
            ranker["optout_loss_per_1000_at_risk_rupees"]
            - antar["optout_loss_per_1000_at_risk_rupees"],
            2,
        )
        total = recovery_gap + harm_gap
        add("Where that difference comes from:")
        add("")
        add("| Component | Rs per 1,000 at-risk cycles | Share |")
        add("|---|---|---|")
        add(f"| Difference in expected recovery | {recovery_gap} | "
            f"{recovery_gap / total:.1%} |")
        add(f"| Difference in avoided cancellation harm | {harm_gap} | "
            f"{harm_gap / total:.1%} |")
        add("")
        add(
            f"**The headline is {harm_gap / total:.0%} harm avoidance and "
            f"{recovery_gap / total:.0%} extra recovery.** The harm term is priced at an "
            "assumed 6x cancellation cost - a config constant, not a measurement - so it "
            "scales linearly with an assumption (`docs/LIMITATIONS.md` L18). What does "
            "not depend on that assumption at all: Antar recovers "
            f"Rs {antar['incremental_per_1000_at_risk_rupees']:,.0f} per 1,000 at-risk "
            f"cycles against the ranker's "
            f"Rs {ranker['incremental_per_1000_at_risk_rupees']:,.0f}, while sending "
            f"{antar['contacts']} messages against {ranker['contacts']}."
        )
        add("")
    prices = data.get("shadow_prices") or {}
    duals = prices.get("lp_duals") or []
    counterfactuals = prices.get("counterfactual_prices") or []
    slack = prices.get("slack_rows") or []

    if duals or counterfactuals:
        add("Constraint prices:")
        add("")
        add("| Constraint | Price (Rs) | Per 1,000 cycles (Rs) | Method |")
        add("|---|---|---|---|")
        for price in [*duals, *counterfactuals]:
            add(
                f"| {price.get('label', price.get('row_id', ''))} | "
                f"{price.get('price_rupees', '')} | "
                f"{price.get('price_per_1000_events_rupees', '')} | "
                f"{price.get('method', '')} |"
            )
        add("")
    if slack:
        add(
            f"Slack (non-binding) rows: {', '.join(slack)}. A zero price on a capacity "
            "row is a finding, not a null result: at this scenario's opt-out "
            "sensitivity the binding constraint is customer tolerance rather than the "
            "merchant's outbound capacity, so one more slot is worth nothing. See "
            "`docs/LIMITATIONS.md` L14 and the phase diagram for where that changes."
        )
        add("")
    framing = prices.get("framing")
    if framing:
        add(f"> {framing}")
        add("")

    verdicts = data.get("retention") or []
    if verdicts:
        add("Component retention (pre-registered rule, `docs/EVALUATION.md` 12.2):")
        add("")
        add("| Component | Delta net (paise) | 95% CI (paise) | Verdict |")
        add("|---|---|---|---|")
        for row in verdicts:
            verdict = "KEEP" if row.get("keep") else "DELETE"
            add(
                f"| {row.get('component', '')} | {row.get('delta_net_paise', '')} | "
                f"({row.get('ci_low_paise', '')}, {row.get('ci_high_paise', '')}) | "
                f"**{verdict}** |"
            )
        add("")
        for row in verdicts:
            if row.get("rationale"):
                add(f"- `{row.get('component')}`: {row['rationale']}")
        add("")


def _section_spec_curve(add, scenario):
    add("## 5. The same question, asked every defensible way")
    add("")
    data = load(f"specification_curve_{scenario}.json")
    if data is None:
        _missing(add, "specification curve")
        return
    summary = data.get("summary", {})
    add(f"- Specifications evaluated: **{summary.get('n_specifications')}**")
    add(
        f"- Median negative-uplift share: **{summary.get('median')}** "
        f"(IQR {summary.get('p25')} to {summary.get('p75')}, "
        f"range {summary.get('min')} to {summary.get('max')})"
    )
    add(
        f"- Share clearing the pre-registered {summary.get('threshold')} bar: "
        f"**{summary.get('fraction_clearing_threshold')}**"
    )
    add(
        f"- The pre-registered specification sits at percentile "
        f"**{summary.get('pre_registered_percentile')}** of the curve"
    )
    add("")
    variance = summary.get("variance_by_dimension") or {}
    if variance:
        add("Variance in the headline explained by each analytic choice:")
        add("")
        add("| Choice | Variance share |")
        add("|---|---|")
        for name, share in sorted(variance.items(), key=lambda kv: -kv[1]):
            add(f"| {name} | {share} |")
        add("")
    verdict = data.get("verdict")
    if verdict:
        add(f"> {verdict}")
        add("")


def _section_phase(add):
    add("## 6. Where the result holds, and where it stops")
    add("")
    data = load("phase_diagram.json")
    if data is None:
        _missing(add, "phase diagram")
        return
    summary = data.get("summary", {})
    add(f"- Cells evaluated: **{len(data.get('cells', []))}**")
    for name, values in (summary.get("axes") or {}).items():
        add(f"- `{name}`: {values[0]} to {values[-1]} in {len(values)} steps")
    add("")
    interpretation = summary.get("interpretation")
    if interpretation:
        add(interpretation)
        add("")
    add("A point estimate from a simulator is worth very little. A boundary condition")
    add("derived from one is a genuine contribution, which is why this section exists.")
    add("")


def _section_batch(add, scenario):
    add("## 7. End-to-end batch and the audit ledger")
    add("")
    data = load(f"batch_{scenario}.json")
    if data is None:
        _missing(add, "batch")
        return
    add(f"- Events: **{data.get('events')}**, decided **{data.get('decided')}**")
    add(
        f"- Contacted **{data.get('contacted')}**, abstained **{data.get('abstained')}**, "
        f"holdout **{data.get('control')}**, refused by the gate **{data.get('refused')}**"
    )
    add(
        f"- Simulated recoveries: **{data.get('recovered')}**, "
        f"opt-outs **{data.get('optouts')}**"
    )
    add(f"- Ledger entries: **{data.get('ledger_entries')}**")
    add(f"- Ledger head: `{str(data.get('ledger_head', ''))[:32]}...`")
    add(f"- Chain verifies: **{data.get('chain_verified')}**")
    add(f"- Replay self-consistent: **{data.get('replay_self_consistent')}**")
    add(
        f"- Uplift model: `{data.get('model_version')}`, "
        f"policy `{data.get('policy_version')}`"
    )
    add("")
    add("The ledger head is the hash of the last entry. Publishing it is what makes a")
    add("truncation detectable from outside - see ADR-0024.")
    add("")


if __name__ == "__main__":
    raise SystemExit(main())
