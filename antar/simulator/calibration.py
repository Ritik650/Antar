"""The calibration report.

docs/SIMULATOR_CARD.md section 9 is candid about what could and could not be
calibrated. This module produces the evidence for both halves:

**What we could check** - that every generated `error_code` / `error_reason` /
`error_source` / `error_step` combination exists in Razorpay's documented taxonomy,
and that the downtime entities and subscription states use real vocabulary.

**What we could not** - actual failure rates, self-heal rates, response rates,
opt-out rates, downtime frequency. All invented. The report says so on every table
that contains one, so a reader skimming it cannot mistake a generated frequency for
an observed one.

Every `[MEASURE @ M2]` placeholder in the simulator card is filled from the output of
this module. None is typed by hand.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from antar.signals import razorpay_errors as rz
from antar.signals.schemas import HIGH_CEILING_CATEGORIES
from antar.simulator import failure_emission
from antar.simulator.generator import SimulatedBatch, batch_summary
from antar.simulator.scenarios import scenario_table


@dataclass
class CalibrationReport:
    """One scenario's calibration evidence."""

    scenario: str
    seed: int
    summary: dict[str, Any]
    error_reason_counts: dict[str, int]
    undocumented_reasons: list[str] = field(default_factory=list)
    """Reasons outside the taxonomy that are **not** deliberate.

    `failure_emission.UNMAPPED_CODES` emits five reasons outside the documented
    taxonomy on purpose - `UNMAPPED_SHARE` of failures - because a real error stream
    contains vendor variants and post-dated codes and a simulator whose every code is
    mappable would flatter the detector. Those are excluded here.

    Anything left is a reason the generator emits and the taxonomy cannot explain,
    which is a defect in one of the two."""

    deliberately_unmapped_reasons: list[str] = field(default_factory=list)
    """The `UNMAPPED_CODES` actually observed. Reported, never counted as dirty."""

    ambiguous_share: float = 0.0
    segment_coverage: dict[str, int] = field(default_factory=dict)
    amount_distribution: dict[str, Any] = field(default_factory=dict)

    @property
    def taxonomy_clean(self) -> bool:
        """No *accidental* gaps. Deliberate ones are a feature and are excluded."""
        return not self.undocumented_reasons

    @property
    def deliberately_unmapped_share(self) -> float:
        total = sum(self.error_reason_counts.values())
        if not total:
            return 0.0
        seen = set(self.deliberately_unmapped_reasons)
        return sum(v for k, v in self.error_reason_counts.items() if k in seen) / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "summary": self.summary,
            "taxonomy_clean": self.taxonomy_clean,
            "undocumented_reasons": self.undocumented_reasons,
            "deliberately_unmapped_reasons": self.deliberately_unmapped_reasons,
            "deliberately_unmapped_share": round(self.deliberately_unmapped_share, 4),
            "ambiguous_code_share": round(self.ambiguous_share, 4),
            "error_reason_counts": dict(sorted(self.error_reason_counts.items())),
            "segment_coverage": dict(sorted(self.segment_coverage.items())),
            "amount_distribution": self.amount_distribution,
        }


def build_report(batch: SimulatedBatch) -> CalibrationReport:
    reasons = Counter(e.error_reason or "" for e in batch.events)

    # The deliberate ones are not a finding. Conflating "outside the taxonomy" with
    # "we failed to document it" made `calibration_report` exit non-zero on every run
    # from the moment UNMAPPED_CODES was introduced - and nothing noticed for three
    # days, because `make evaluate` had never been run end to end. POSTMORTEM D31.
    deliberate = failure_emission.UNMAPPED_REASONS
    undocumented = sorted(
        r for r in reasons if r and r not in deliberate and not rz.is_documented(r)
    )
    observed_deliberate = sorted(r for r in reasons if r in deliberate)
    ambiguous = failure_emission.ambiguous_reasons()
    ambiguous_count = sum(count for reason, count in reasons.items() if reason in ambiguous)

    amounts = sorted(e.amount_paise for e in batch.events)
    above = [e for e in batch.events if _above_ceiling(e)]

    return CalibrationReport(
        scenario=batch.scenario.name,
        seed=batch.seed,
        summary=batch_summary(batch),
        error_reason_counts=dict(reasons),
        undocumented_reasons=undocumented,
        deliberately_unmapped_reasons=observed_deliberate,
        ambiguous_share=(ambiguous_count / len(batch.events)) if batch.events else 0.0,
        segment_coverage=dict(Counter(e.segment_key for e in batch.events)),
        amount_distribution={
            "count": len(amounts),
            "min_paise": amounts[0] if amounts else 0,
            "p25_paise": _quantile(amounts, 0.25),
            "median_paise": _quantile(amounts, 0.50),
            "p75_paise": _quantile(amounts, 0.75),
            "p99_paise": _quantile(amounts, 0.99),
            "max_paise": amounts[-1] if amounts else 0,
            "above_afa_ceiling": len(above),
            "above_afa_ceiling_share": round(len(above) / max(len(amounts), 1), 4),
        },
    )


def _quantile(values: list[int], q: float) -> int:
    if not values:
        return 0
    index = min(int(q * (len(values) - 1)), len(values) - 1)
    return values[index]


def _above_ceiling(event: Any) -> bool:
    ceiling = 10_000_000 if event.merchant_category in HIGH_CEILING_CATEGORIES else 1_500_000
    return event.amount_paise > ceiling


CALIBRATED_AGAINST = [
    (
        "Failure-class label structure and payload shape",
        "Razorpay published error taxonomy",
        "Checked. Every generated error_code/reason/source/step tuple is a row of "
        "antar/signals/razorpay_errors.py, asserted by test_taxonomy_realism.",
    ),
    (
        "Downtime entity structure",
        "Razorpay Downtime API documentation",
        "Checked structurally: severity values, method semantics, and the "
        "scheduled/unscheduled distinction. Frequencies are invented.",
    ),
    (
        "Subscription lifecycle states",
        "Razorpay subscription documentation",
        "Checked. The mandate FSM is exercised against real state names.",
    ),
    (
        "Regulatory thresholds",
        "RBI e-mandate framework; TRAI TCCCPR",
        "Rs 15,000 / Rs 1,00,000 AFA ceilings, the 24-hour lead time, and the "
        "09:00-21:00 window are cited, not invented. See REGULATORY_REGISTER.md.",
    ),
]

NOT_CALIBRATED = [
    "Failure rates by issuer, method, or segment",
    "Self-healing rates",
    "Response rates to dunning, by channel",
    "Opt-out rates following pre-debit notifications",
    "Downtime frequency and duration in production",
    "Any real customer behaviour whatsoever",
]


def render_markdown(reports: list[CalibrationReport]) -> str:
    """A human-readable report. Written to artifacts/, not committed as prose."""
    lines: list[str] = [
        "# Calibration report",
        "",
        "Generated by `python tasks.py calibration-report`. Do not edit by hand.",
        "",
        "> **Every frequency in this report is invented.** The structure is real: the",
        "> error vocabulary, the downtime entity shape, the subscription states, and the",
        "> regulatory thresholds are all taken from documentation. The *numbers* are",
        "> properties of parameters we chose, and are not estimates of what any real",
        "> merchant would see. docs/SIMULATOR_CARD.md section 9.",
        "",
        "## 1. What could be calibrated",
        "",
        "| Aspect | Source | Status |",
        "|---|---|---|",
    ]
    for aspect, source, status in CALIBRATED_AGAINST:
        lines.append(f"| {aspect} | {source} | {status} |")

    lines += [
        "",
        "## 2. What could not be calibrated",
        "",
        *[f"- {item}" for item in NOT_CALIBRATED],
        "",
        "## 3. Scenario parameters",
        "",
        "| Parameter | " + " | ".join(row["name"] for row in scenario_table()) + " |",
        "|---" * (len(scenario_table()) + 1) + "|",
    ]
    table = scenario_table()
    for key in table[0]:
        if key == "name":
            continue
        lines.append(f"| `{key}` | " + " | ".join(str(row[key]) for row in table) + " |")

    lines += ["", "## 4. Generated batches", ""]
    for report in reports:
        summary = report.summary
        lines += [
            f"### {report.scenario} (seed {report.seed})",
            "",
            f"- attempts: **{summary['attempts']:,}**, "
            f"first-attempt failure rate: **{summary['first_attempt_failure_rate']:.1%}** (invented)",
            f"- at-risk events: **{summary['at_risk_events']:,}** "
            f"({summary['mandate_failures']:,} mandate failures, "
            f"{summary['checkout_abandons']:,} checkout abandonments)",
            f"- downtime windows injected: **{summary['downtime_windows']}**",
            f"- at-risk value: **Rs {summary['at_risk_value_paise'] / 100:,.0f}**",
            f"- above the AFA ceiling: **{summary['above_afa_ceiling']:,}** "
            f"({summary['above_afa_ceiling_share']:.1%})",
            f"- events carrying an ambiguous error code: **{report.ambiguous_share:.1%}** "
            "(the share the taxonomy table alone cannot resolve)",
            f"- taxonomy clean: **{report.taxonomy_clean}**"
            + (
                ""
                if report.taxonomy_clean
                else f" - UNDOCUMENTED: {report.undocumented_reasons}"
            ),
            "",
            "| True cause | Share of mandate failures |",
            "|---|---|",
        ]
        for cause, share in summary["mandate_failure_class_shares"].items():
            lines.append(f"| `{cause}` | {share:.1%} |")
        lines.append("")

    return "\n".join(lines) + "\n"
