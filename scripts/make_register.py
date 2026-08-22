"""Generate docs/REGULATORY_REGISTER.md from antar/policy/regulations.py.

    python -m scripts.make_register           # write
    python -m scripts.make_register --check   # fail if out of date (CI, pre-commit)

The document is generated so that code and prose cannot diverge. A register that
claims a rule the code does not enforce is worse than no register, because it invites
a reader to trust an enforcement that is not there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from antar.policy.regulations import (
    REGULATIONS,
    Severity,
    Verification,
    summarise,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "REGULATORY_REGISTER.md"

VERIFICATION_BADGE = {
    Verification.PRIMARY: "**primary**",
    Verification.REPRODUCTION: "reproduction",
    Verification.SECONDARY: "_secondary_",
    Verification.UNVERIFIED: "**UNVERIFIED**",
}


def render() -> str:
    stats = summarise()
    lines: list[str] = [
        "# REGULATORY_REGISTER.md",
        "",
        "<!-- GENERATED FILE - DO NOT EDIT.",
        "     Source: antar/policy/regulations.py",
        "     Regenerate: python -m scripts.make_register",
        "     CI fails if this file and the code disagree. -->",
        "",
        "Every rule Antar obeys, as encoded. N5 requires that regulatory constraints "
        "live as data with a citation field and never as a hard-coded value inside "
        "business logic; this document is that data, rendered.",
        "",
        "## Verification status",
        "",
        f"- **{stats.total}** rules: {stats.blocking} blocking, {stats.advisory} advisory",
        f"- Verified against a primary regulator document: **{stats.primary}**",
        f"- Verified against a full-text gazette reproduction: **{stats.reproduction}**",
        f"- Resting on secondary sources (law-firm notes, trade press): "
        f"**{stats.secondary}** — all advisory",
        f"- Unverified: **{stats.unverified}**",
        "",
        "**A rule may only be `BLOCKING` if it was checked against a primary document "
        "or a full-text reproduction.** A regulation resting on a summary cannot stop "
        "money; it can only be reported. `test_blocking_rules_are_verified` enforces "
        "this, so the rule cannot be quietly relaxed.",
        "",
    ]

    if stats.corrections:
        lines += [
            "## Corrections to PLAN.md",
            "",
            "Verification was done at encoding time rather than before submission, on "
            "the grounds that a threshold which turns out to differ changes the "
            "constraint rows and everything built on them. It found "
            f"**{len(stats.corrections)}** place(s) where the plan, compiled from "
            "secondary sources, was wrong or overstated:",
            "",
        ]
        for rule_id in stats.corrections:
            rule = next(r for r in REGULATIONS if r.id == rule_id)
            lines.append(f"- **{rule_id}** — {rule.title}")
        lines += [
            "",
            "Each is detailed in its entry below. The most consequential is `TRAI-01`: "
            "the permitted contact window opens at **10:00**, not 09:00, because the "
            "08:00-10:00 band is default-OFF for every customer. Antar has an hour a "
            "day less contact capacity than the plan assumed — which raises the shadow "
            "price on a contact slot rather than lowering it.",
            "",
        ]

    lines += ["## Rules", ""]

    for rule in REGULATIONS:
        badge = VERIFICATION_BADGE[rule.verification]
        marker = "[BLOCKING]" if rule.severity is Severity.BLOCKING else "[advisory]"
        lines += [
            f"### {marker} `{rule.id}` — {rule.title}",
            "",
            "| | |",
            "|---|---|",
            f"| Severity | `{rule.severity.value}` |",
            f"| Source | {rule.source} |",
            f"| Clause | {rule.clause} |",
            f"| Effective | {rule.effective_date.isoformat()} |",
            f"| Verification | {badge}"
            + (f", checked {rule.verified_on.isoformat()}" if rule.verified_on else "")
            + " |",
            f"| Citation | <{rule.citation_url}> |",
            f"| Applies to | {'contact actions only' if rule.applies_to_contacts_only else 'every action, including silent retries'} |",
            "",
        ]
        if rule.quote:
            lines += ["> " + rule.quote.replace("\n", " "), ""]
        lines += [rule.human_explanation, ""]
        if rule.note:
            lines += [f"**Note.** {rule.note}", ""]

    lines += [
        "---",
        "",
        "## What this register does not cover",
        "",
        "- **Per-customer preference registrations** (`TRAI-08`). Antar consumes no "
        "preference feed, so registered time-band, day-of-week and holiday preferences "
        "are not honoured. A production deployment must consume that feed before any "
        "of this can be called compliant.",
        "- **Number-series allocation** (`TRAI-05`). Antar places no real calls, so no "
        "series is ever allocated and the rule is never exercised end to end.",
        "- **DLT template registration.** Templates here are DLT-*shaped*; none is "
        "registered with an access provider.",
        "- **Jurisdictions other than India.** Every rule assumes an Indian customer "
        "and IST. `CustomerContext.timezone_offset_minutes` is the seam where that "
        "would change.",
        "",
        "See `docs/LIMITATIONS.md` for the full list.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if out of date")
    args = parser.parse_args()

    rendered = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""

    if args.check:
        if current != rendered:
            print(
                "docs/REGULATORY_REGISTER.md is out of date with "
                "antar/policy/regulations.py.\nRun: python -m scripts.make_register",
                file=sys.stderr,
            )
            return 1
        print("regulatory register is in sync")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    stats = summarise()
    print(
        f"wrote {TARGET.relative_to(ROOT)} "
        f"({stats.total} rules, {stats.blocking} blocking, "
        f"{len(stats.corrections)} corrections to PLAN.md)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
