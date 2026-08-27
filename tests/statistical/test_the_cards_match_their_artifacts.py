"""The simulator card states results. Nothing checked them against the results.

`test_results_are_reproducible.py` enforces provenance on **the README**: every number in
a results table there is an artifact value, or a rounding or unit conversion of one. That
guard is good and it is narrow, and `docs/SIMULATOR_CARD.md` sat outside it.

So the card drifted. Three separate things were true in the repo at once:

  * `artifacts/claims.json` recorded the sleeping-dogs claim as **withdrawn**, base share
    2.67% against a 5% bar;
  * the card's §6.3.1 reported base **5.8%**, "clears the 5% bar by 0.8 points";
  * the card's §6.3.2 said in bold, *"The base-scenario result is a finding, not a coin
    flip"* — the withdrawn claim, asserted, in the document whose own §10 defines what
    withdrawal means.

`test_withdrawn_claims_are_not_stated.py` did not catch it because its
`ASSERTING_DOCUMENTS` list excluded the card, and its patterns match phrasings like "we
detect sleeping dogs" rather than a bolded conclusion. §15 had drifted the same way after
the phase-diagram grid was regenerated without its post-hoc extension: 264 cells stated,
176 committed; a boundary quoted at 0.035 that the committed artifact explicitly says it
cannot locate.

## The instrument

This file contains **no expected values**. Each check reads a figure from an artifact,
formats it the way the card formats it, and asserts that string appears in the card row
that claims it. A number typed into a test is the same defect one level up.

That makes the failure mode loud and the fix obvious: regenerate the artifact, or correct
the row. It cannot be satisfied by editing this file, because there is nothing here to
edit.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from antar.config import artifacts_dir, repo_root

REQUIRE_ARTIFACTS = os.environ.get("ANTAR_REQUIRE_ARTIFACTS") == "1"

CARD = "docs/SIMULATOR_CARD.md"


def card() -> str:
    path = repo_root() / CARD
    if not path.exists():
        pytest.fail(f"{CARD} is missing; it is the document N6 points every reader at")
    return path.read_text(encoding="utf-8")


def artifact(name: str) -> dict:
    path = artifacts_dir() / name
    if not path.exists():
        if REQUIRE_ARTIFACTS:
            pytest.fail(
                f"ANTAR_REQUIRE_ARTIFACTS=1 but artifacts/{name} is absent. "
                "`python tasks.py evaluate` is supposed to write it."
            )
        pytest.skip(f"no {name}; run `python tasks.py evaluate`")
    return json.loads(path.read_text(encoding="utf-8"))


def row(label: str) -> str:
    """The one table row in the card whose first cell matches `label`.

    Fails rather than skips when the row is gone: a check that silently stops checking
    is how a guard becomes decoration, which is the shape of half this project's
    postmortem.
    """
    pattern = re.compile(rf"^\|[^|\n]*{re.escape(label)}[^|\n]*\|.*$", re.M)
    matches = pattern.findall(card())
    assert matches, (
        f"{CARD} has no table row labelled {label!r}. If the section was rewritten, "
        "update this check with it - do not delete it."
    )
    assert len(matches) == 1, f"{label!r} matches {len(matches)} rows in {CARD}: {matches}"
    return matches[0]


def assert_states(label: str, expected: str, why: str) -> None:
    line = row(label)
    assert expected in line, (
        f"{CARD} row {label!r} does not state {expected!r}.\n"
        f"  the row says: {line.strip()}\n"
        f"  the artifact says: {expected}  ({why})\n"
        "Either the card is stale or the artifact is. Run `python tasks.py evaluate`."
    )


# --------------------------------------------------- 6.3.1, the claim itself


def test_the_card_reports_the_negative_uplift_shares_the_registry_recorded():
    """§6.3.1's table against `claims.json`, scenario by scenario."""
    shares = artifact("claims.json")["sleeping_dogs"]["evidence"]["share_by_scenario"]
    for scenario, share in shares.items():
        assert_states(
            f"`{scenario}`",
            f"{share:.2%}",
            f"claims.json share_by_scenario[{scenario}]",
        )


def test_the_card_does_not_assert_a_claim_its_own_registry_withdrew():
    """The card defines withdrawal in §10. It must not then assert the claim in §6.

    Deliberately checked as a *conclusion*, not a phrasing. The sentence that survived
    three review cycles was "The base-scenario result is a finding, not a coin flip" -
    which contains none of the words `test_withdrawn_claims_are_not_stated.py` looks
    for.
    """
    verdict = artifact("claims.json")["sleeping_dogs"]
    text = card()
    if verdict["supported"]:
        return

    forbidden = re.compile(
        r"\bis a finding,? not a coin flip\b|\bthe majority rule in .?9\.5 is satisfied\b",
        re.I,
    )
    hits = [line.strip() for line in text.splitlines() if forbidden.search(line)]
    assert not hits, (
        "the sleeping-dogs claim is WITHDRAWN in artifacts/claims.json "
        f"({verdict['detail']}) and {CARD} still concludes the opposite:\n  " + "\n  ".join(hits)
    )

    assert "WITHDRAWN" in text, (
        f"{CARD} §6.3.1 should say the claim is withdrawn. §10 of this same document "
        "makes withdrawal the stated consequence of the anti-circularity test failing, "
        "so the card is the last place that should be silent about it."
    )


# ------------------------------------------- 6.3.2, the specification curve


def test_the_card_reports_the_specification_curve_the_artifact_produced():
    summary = artifact("specification_curve_base.json")["summary"]

    assert_states("Median share", f"{summary['median']:.2%}", "summary.median")
    assert_states(
        "Clearing the 5% bar",
        f"{summary['fraction_clearing_threshold']:.0%}",
        "summary.fraction_clearing_threshold",
    )
    assert_states(
        "Pre-registered specification",
        f"{summary['pre_registered_share']:.2%}",
        "summary.pre_registered_share",
    )
    assert_states(
        "Pre-registered specification",
        f"{summary['pre_registered_percentile']}",
        "summary.pre_registered_percentile",
    )
    assert_states(
        "Specifications",
        f"{summary['n_specifications']}",
        "summary.n_specifications",
    )


# ---------------------------------------------------- 15, the phase diagram


def test_the_card_reports_the_phase_diagram_the_artifact_produced():
    """§15's comparison table, both panels, against `phase_diagram.json`.

    Every row here was wrong at once after the grid was regenerated: 83%/75% decisive
    against a measured 51%/51%, and a median advantage overstated roughly threefold.
    Nothing failed, because nothing was looking.
    """
    phase = artifact("phase_diagram.json")
    panels = phase["summary"]["panels"]

    for key, label, fmt in (
        ("decisive_win_share", "Decisive (outside the indifference band)", "{:.0%}"),
        ("antar_wins_share", "Ahead, as a share of cells", "{:.0%}"),
        ("capacity_binds_share", "Capacity binds in", "{:.0%}"),
    ):
        for name, panel in panels.items():
            assert_states(label, fmt.format(panel[key]), f"panels[{name}].{key}")

    for name, panel in panels.items():
        assert_states(
            "Median advantage per 1,000 cycles",
            f"{panel['median_delta_per_1000_rupees']:,.0f}",
            f"panels[{name}].median_delta_per_1000_rupees",
        )
        assert_states(
            "Contact capacity binds up to",
            str(panel["capacity_binding_ceiling_optout"]),
            f"panels[{name}].capacity_binding_ceiling_optout",
        )


def test_the_card_does_not_quote_a_boundary_the_grid_never_located():
    """`decisive_boundary_optout` is `null`. A number in that row would be invented.

    This is the D35/D36 shape - a generated or transcribed sentence with no branch for
    "unknown" - applied to a document instead of to a template.
    """
    panels = artifact("phase_diagram.json")["summary"]["panels"]
    located = {
        name: panel["decisive_boundary_optout"]
        for name, panel in panels.items()
        if panel.get("decisive_boundary_optout") is not None
    }
    line = row("Decisive at every self-heal level from")

    if not located:
        assert "not located" in line.lower(), (
            "no panel in phase_diagram.json locates a decisive boundary "
            "(`decisive_boundary_optout` is null in every panel), but the card states "
            f"one:\n  {line.strip()}\n"
            "Say the grid does not contain it. An unknown rendered as a number is the "
            "defect, not the unknown."
        )
        return

    for name, value in located.items():
        assert str(value) in line, (
            f"panels[{name}] locates the boundary at {value}; the row does not say so"
        )


def test_the_readme_states_the_number_of_postmortem_entries_it_has():
    """A hand-counted total in prose is the same defect in miniature.

    The README says the postmortem "has N entries". Nothing counted them, so N was
    whatever it was when someone last remembered - and an under-count in a document
    whose argument is that its statements match its evidence is a bad place to be
    approximate.
    """
    readme = (repo_root() / "README.md").read_text(encoding="utf-8")
    postmortem = (repo_root() / "docs" / "POSTMORTEM.md").read_text(encoding="utf-8")

    entries = len(re.findall(r"^## D\d+", postmortem, re.M))
    stated = re.search(r"POSTMORTEM\.md\) has (\d+) entries", readme)
    assert stated, "the README no longer states a postmortem entry count in the form it did"
    assert int(stated.group(1)) == entries, (
        f"the README says the postmortem has {stated.group(1)} entries; it has {entries}."
    )


def test_the_card_reports_the_grid_it_actually_has():
    """The stated cell count against the cells in the file.

    It said 264 while the artifact held 176 - a grid regenerated without the post-hoc
    extension the section's headline finding rested on.
    """
    phase = artifact("phase_diagram.json")
    stated = re.search(r"^([\d,]+) cells:", card(), re.M)
    assert stated, f"{CARD} §15 does not state a cell count in the form `N cells:`"
    assert int(stated.group(1).replace(",", "")) == len(phase["cells"]), (
        f"{CARD} §15 says {stated.group(1)} cells; phase_diagram.json holds "
        f"{len(phase['cells'])}. Regenerate the grid or correct the section."
    )
