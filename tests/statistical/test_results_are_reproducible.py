"""N4, made checkable.

> `make evaluate` from a clean checkout reproduces every number in the README.

A promise like that decays the moment someone edits a figure in prose. So this file
extracts every number the README quotes as a *result* and checks it appears in an
artifact `python tasks.py evaluate` produced.

## What it does not attempt

It does not parse English. It extracts numeric literals from the README's results
tables and bolded claims, normalises them (commas, currency symbols, percent signs,
minus signs of three different Unicode flavours), and looks for each one among the
numbers reachable in `artifacts/*.json`.

That is a **necessary** condition, not a sufficient one: a number can appear in an
artifact under a completely different meaning, as D24 proved at some cost. The
companion file `test_artifacts_are_self_consistent.py` is what checks meaning. This one
checks provenance — that no figure in the README was typed rather than measured.

`RESULTS.md` is exempt from nothing: it is generated, so every number in it is by
construction from an artifact, and this file checks the README against the artifacts
directly rather than trusting the intermediate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from antar.config import artifacts_dir, repo_root

README = repo_root() / "README.md"

# Numbers that are structural rather than measured: version numbers, section counts,
# regulation ids, years, ports, and the small integers that appear in prose like "three
# policies". Each entry is here because it is *not* a claim about a result.
STRUCTURAL = {
    # Layer and milestone labels, counts of things in the repo rather than measurements.
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15",
    "16", "17", "18", "24",
    # Years, ceilings and clock hours quoted from regulation, not measured by us.
    "2026", "03", "15000", "21", "0", "1000",
    # AFA ceiling in rupees, and the TRAI window hours.
    "15,000",
}

# Built from code points rather than literals: ruff flags a bare U+2212 as an
# ambiguous character, and it is right to - which is exactly why the README uses
# one and this file has to handle it.
MINUS = chr(0x2212)
ENDASH = chr(0x2013)
NUMBER = re.compile(rf"[-{MINUS}{ENDASH}]?\s?₹?\s?\d[\d,]*(?:\.\d+)?%?")


def normalise(token: str) -> str:
    """One canonical string per numeric value, so 1,126,509 and 1126509.0 match."""
    cleaned = (
        token.replace("₹", "")
        .replace(",", "")
        .replace("%", "")
        .replace(MINUS, "-")
        .replace(ENDASH, "-")
        .replace(" ", "")
        .strip()
    )
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    return f"{value:.6g}"


CODE_SPAN = re.compile(r"`[^`]*`")
CLOCK = re.compile(r"[0-9]{1,2}:[0-9]{2}")
"""Wall-clock times. `02:44` in the provenance table is when a commit was made and
`10:00-21:00` is a regulatory window; neither is a measured result, and both would
otherwise be demanded of the artifacts."""


def numbers_in(text: str) -> set[str]:
    """Numbers stated as *results*, with identifiers removed first.

    Inline code spans and wall-clock times are stripped before extraction. A digit
    inside backticks is part of a name - a commit hash, a model version like
    `x_learner-n541`, a policy hash - and `02:44` is when something happened. Neither is
    a measurement. Without this the provenance table read as a wall of unexplained
    figures, which is the kind of false positive that trains someone to ignore a test.
    """
    cleaned = CLOCK.sub(" ", CODE_SPAN.sub(" ", text))
    return {normalise(match.group()) for match in NUMBER.finditer(cleaned)}


def _walk(node: object, sink: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk(value, sink)
    elif isinstance(node, list):
        for value in node:
            _walk(value, sink)
    elif isinstance(node, bool):
        return
    elif isinstance(node, int | float):
        sink |= readings_of(float(node))
    elif isinstance(node, str):
        sink |= expand(numbers_in(node))


def readings_of(value: float) -> set[str]:
    """Every reading of a stored figure that is a *unit change or a rounding*.

    Never a reading that changes the claim. Paise to rupees is not a claim; a different
    number is.
    """
    if value != value or value in (float("inf"), float("-inf")):
        # A NaN in an artifact is a real thing (an unfilled phase-diagram cell) and is
        # not a number the README could be quoting.
        return set()

    out: set[str] = set()
    for reading in (value, value / 100, value * 100):
        out.add(f"{reading:.6g}")
        for places in (0, 1, 2, 3):
            out.add(f"{round(reading, places):.6g}")
        # Percentages: 0.9168 in an artifact reads as "91.7%" or "0.917" in prose.
        out.add(f"{round(reading * 100, 1):.6g}")
    return out


def expand(tokens: set[str]) -> set[str]:
    """Numbers found in *text* get the same treatment as numbers found in JSON.

    `RESULTS.md` is generated, so a figure printed there as `6322.08` is as much a
    measured number as one stored as `6322.08` in JSON - and the README rounding it to
    `6,322` is a rounding, not a fabrication."""
    out = set(tokens)
    for token in tokens:
        try:
            out |= readings_of(float(token))
        except ValueError:
            continue
    return out


def artifacts_are_from_a_quick_run() -> bool:
    """Did the artifacts on disk come from `evaluate QUICK=1`?

    QUICK mode is smaller batches on the same code path, and `run_evaluation` says in
    its own banner that the numbers will differ from the full run. The README quotes the
    **full** run, so checking it against quick artifacts compares two things that are
    not supposed to agree - which is what this test did on CI's N4 job, where it failed
    on every correctly-generated number. POSTMORTEM D37.

    Provenance is only meaningful against a full run. Internal consistency and magnitude
    hold at any scale, and those tests keep running.
    """
    marker = artifacts_dir() / "evaluation.json"
    if not marker.exists():
        return False
    try:
        return bool(json.loads(marker.read_text(encoding="utf-8")).get("quick"))
    except json.JSONDecodeError:
        return False


@pytest.fixture(scope="module")
def artifact_numbers() -> set[str]:
    files = sorted(artifacts_dir().glob("*.json"))
    if not files:
        pytest.skip("no artifacts; run `python tasks.py evaluate`")
    sink: set[str] = set()
    for path in files:
        try:
            _walk(json.loads(path.read_text(encoding="utf-8")), sink)
        except json.JSONDecodeError:
            continue
    results = artifacts_dir() / "RESULTS.md"
    if results.exists():
        sink |= expand(numbers_in(results.read_text(encoding="utf-8")))
    return sink


def result_lines() -> list[tuple[int, str]]:
    """Lines that state a result: table rows and bolded claims.

    Prose is excluded deliberately. "Seventeen entries" and "one click" are not results,
    and a regex that tried to tell them apart from measurements would be a worse
    heuristic than a structural one.
    """
    lines: list[tuple[int, str]] = []
    in_code = False
    for number, raw in enumerate(README.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith(">"):
            continue
        if line.startswith("|") and not set(line) <= set("|-: "):
            lines.append((number, line))
    return lines


def test_the_readme_quotes_results():
    """If the extractor stops finding anything, every test below passes vacuously."""
    assert result_lines(), "no result rows found in the README"


def test_every_number_in_a_results_table_exists_in_an_artifact(artifact_numbers):
    """N4, stated precisely: **every number in a README results table is an artifact
    value, or a rounding or unit conversion of one.**

    Not "appears verbatim in an artifact". `allocation_base.json` stores `84694.6` and
    the README says `₹84,695`; the artifact stores paise and the prose says rupees. Both
    are the same measurement, and forbidding either would make the README unreadable
    without making it more honest.

    What the tolerance deliberately does *not* admit is a different number. `readings_of`
    generates only unit changes (x100, /100) and roundings to four decimal places. A
    figure that needs any other transformation to match is a figure nobody measured.

    An external reviewer noticed that `84695` does not literally `grep` out of any JSON
    and asked whether the guard's claim matched its enforcement. It did not — the
    enforcement was right and the wording was loose. POSTMORTEM D40.
    """
    if artifacts_are_from_a_quick_run():
        pytest.skip(
            "artifacts came from `evaluate QUICK=1`, whose numbers differ from the full "
            "run the README quotes. Provenance is checked against a full run; the "
            "consistency and magnitude tests still run here."
        )

    unexplained: list[str] = []
    for line_number, line in result_lines():
        for token in numbers_in(line):
            if token in STRUCTURAL or token in artifact_numbers:
                continue
            unexplained.append(f"README.md:{line_number}  {token}   in: {line[:90]}")

    assert not unexplained, (
        "these numbers in the README are not an artifact value, nor a rounding or "
        "unit conversion of one. Either they were typed by hand, or the artifact that "
        "produced them is stale. Run `python tasks.py evaluate`.\n  " + "\n  ".join(unexplained)
    )


def test_the_readme_says_the_numbers_are_simulated(artifact_numbers):
    """N6, on the document most likely to be read alone."""
    text = README.read_text(encoding="utf-8")
    assert "simulated" in text.lower()
    assert "SIMULATOR_CARD" in text
    # In the first screen, not buried at the bottom.
    assert "simulated" in text[:1500].lower(), (
        "the simulation caveat is below the fold; a number travels further than its "
        "caveat"
    )


def test_the_readme_links_the_documents_it_relies_on():
    text = README.read_text(encoding="utf-8")
    for document in (
        "docs/LIMITATIONS.md",
        "docs/POSTMORTEM.md",
        "docs/DECISIONS.md",
        "docs/SIMULATOR_CARD.md",
        "docs/REGULATORY_REGISTER.md",
    ):
        assert document in text, f"the README does not link {document}"


def test_the_linked_documents_exist():
    """A broken link in the README is the first thing a reviewer clicks."""
    text = README.read_text(encoding="utf-8")
    for match in re.finditer(r"\]\((docs/[^)#]+)\)", text):
        target = repo_root() / match.group(1)
        assert target.exists(), f"README links {match.group(1)}, which does not exist"


def test_the_quickstart_commands_are_real_targets():
    """A README that tells a stranger to run a target that does not exist is worse than
    one with no quickstart."""
    import tasks

    text = README.read_text(encoding="utf-8")
    referenced = set(re.findall(r"python tasks\.py ([a-z-]+)", text))
    unknown = sorted(referenced - set(tasks.TARGETS))
    assert not unknown, f"the README names targets that do not exist: {unknown}"


def test_the_readme_states_every_non_negotiable():
    text = README.read_text(encoding="utf-8")
    for marker in ("N1", "N2", "N3", "N4", "N5", "N6"):
        assert f"**{marker}**" in text, f"{marker} is not stated in the README"


def test_readme_limitation_references_resolve():
    """Every `L<n>` the README cites must be a heading in LIMITATIONS.md."""
    text = README.read_text(encoding="utf-8")
    limitations = (repo_root() / "docs" / "LIMITATIONS.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (L\d+)", limitations, flags=re.MULTILINE))
    cited = set(re.findall(r"\b(L1[0-9]|L[1-9])\b", text))
    missing = sorted(cited - headings)
    assert not missing, f"the README cites limitations that do not exist: {missing}"


def test_readme_postmortem_references_resolve():
    text = README.read_text(encoding="utf-8")
    postmortem = (repo_root() / "docs" / "POSTMORTEM.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (D\d+)", postmortem, flags=re.MULTILINE))
    cited = set(re.findall(r"\b(D[1-9][0-9]?)\b", text))
    missing = sorted(cited - headings)
    assert not missing, f"the README cites postmortem entries that do not exist: {missing}"


def test_readme_adr_references_resolve():
    text = README.read_text(encoding="utf-8")
    decisions = (repo_root() / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (ADR-\d+)", decisions, flags=re.MULTILINE))
    cited = set(re.findall(r"\b(ADR-\d{4})\b", text))
    missing = sorted(cited - headings)
    assert not missing, f"the README cites ADRs that do not exist: {missing}"


def test_the_repo_map_names_directories_that_exist():
    text = README.read_text(encoding="utf-8")
    for match in re.finditer(r"^\| `(antar/[^`]+|tests/|docs/|artifacts/)`", text, re.MULTILINE):
        path = Path(repo_root()) / match.group(1)
        assert path.exists(), f"the repo map names {match.group(1)}, which does not exist"


# --------------------------------------- counts the README states about itself


WORDS = {
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23, "twenty-four": 24,
    "twenty-five": 25, "twenty-six": 26,
}


def _stated(pattern: str) -> int | None:
    """The count the README claims, whether written as a word or a digit."""
    match = re.search(pattern, README.read_text(encoding="utf-8"), flags=re.IGNORECASE)
    if match is None:
        return None
    token = match.group(1).lower()
    return WORDS.get(token, int(token) if token.isdigit() else None)


def _headings(document: str, prefix: str) -> int:
    text = (repo_root() / "docs" / document).read_text(encoding="utf-8")
    return len(re.findall(rf"^## {prefix}", text, flags=re.MULTILINE))


def test_the_readme_states_the_right_number_of_postmortem_entries():
    """A count in prose rots the moment the document grows. This is the cheapest
    possible way to notice - and the README's credibility rests on these documents
    being exactly as complete as it says."""
    stated = _stated(r"POSTMORTEM\.md\) has ([\w-]+) entries")
    assert stated is not None, "the README no longer states a postmortem count"
    assert stated == _headings("POSTMORTEM.md", "D"), (
        f"the README says {stated} postmortem entries; there are "
        f"{_headings('POSTMORTEM.md', 'D')}"
    )


def test_the_readme_states_the_right_number_of_limitations():
    stated = _stated(r"LIMITATIONS\.md\) . ([\w-]+) entries")
    assert stated is not None, "the README no longer states a limitations count"
    assert stated == _headings("LIMITATIONS.md", "L"), (
        f"the README says {stated} limitations; there are {_headings('LIMITATIONS.md', 'L')}"
    )


def test_the_readme_states_the_right_number_of_regulations():
    from antar.policy import regulations as reg

    stated = _stated(r"([\w-]+) regulations are encoded as data")
    assert stated is not None, "the README no longer states a regulation count"
    assert stated == len(reg.REGULATIONS), (
        f"the README says {stated} regulations; there are {len(reg.REGULATIONS)}"
    )


def test_the_tolerance_admits_roundings_and_rejects_different_numbers():
    """The rule's boundary, asserted rather than described.

    The wording says the guard admits a rounding or a unit conversion "and nothing else".
    That sentence is only worth writing if something checks it, so this pins both sides:
    a legitimate reading is accepted, and a number that merely *looks* close is not.
    """
    stored = readings_of(84694.6)

    # What the README legitimately prints.
    assert normalise("84,695") in stored
    assert normalise("₹84,694.60") in stored
    # Unit conversion: the same figure in paise.
    assert normalise("8469460") in stored

    # And what it may not. Each of these is a *different measurement*, and a guard that
    # waved them through would be checking nothing.
    for impostor in ("84,700", "8,469", "846,946", "84,695.5"):
        assert normalise(impostor) not in stored, (
            f"{impostor} is not a rounding or unit conversion of 84694.6, and the "
            "tolerance must not accept it"
        )


# ------------------------------------------------- the rule states itself once

PROVENANCE_RULE = "an artifact value, or a rounding or unit conversion of one"
"""The canonical wording. Every site that describes the provenance guard uses it
verbatim, and `test_the_provenance_rule_is_worded_identically_everywhere` fails when any
one of them drifts."""

RULE_SITES = (
    "README.md",
    "docs/SUBMISSION.md",
    "docs/DECISIONS.md",
    "scripts/run_evaluation.py",
    "tests/statistical/test_magnitudes_are_plausible.py",
    "tests/statistical/test_results_are_reproducible.py",
)


def test_the_provenance_rule_is_worded_identically_everywhere():
    """D40, one layer down.

    D40 corrected the guard's description from "appears in an artifact" to what it
    actually enforces — and then the postmortem entry claimed the new wording was
    "stated identically in four places" when two of the four edits had silently done
    nothing. A bare `str.replace` does not fail on a missing anchor.

    So the claim of uniformity is now the thing under test. Five lines, and it closes
    the class: any site that drifts, or any new site that describes the rule loosely,
    fails here with its own name.
    """
    missing = [
        site
        for site in RULE_SITES
        if PROVENANCE_RULE not in (repo_root() / site).read_text(encoding="utf-8")
    ]
    assert not missing, (
        f"these files describe the provenance guard without the canonical wording "
        f"{PROVENANCE_RULE!r}:\n  " + "\n  ".join(missing)
        + "\n\nThe postmortem claims the rule is stated identically everywhere. This is "
        "what makes that claim true rather than asserted."
    )


def test_the_superseded_wording_is_gone():
    """The other half: the loose sentence D40 removed must not come back.

    Two exemptions, both structural rather than convenient:

      * `docs/POSTMORTEM.md` — D40 quotes the old wording in order to explain what was
        wrong with it, and an entry that could not quote the defect could not describe it.
      * **This file** — it has to name the forbidden phrases in order to search for them.
        A check cannot be its own violation.
    """
    exempt = {"POSTMORTEM.md", Path(__file__).name}
    offenders: list[str] = []
    for path in sorted(repo_root().rglob("*.py")) + sorted(repo_root().rglob("*.md")):
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        if path.name in exempt:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for phrase in ("appears in an artifact", "appear in an artifact"):
            if phrase in text:
                offenders.append(f"{path.relative_to(repo_root())}  ({phrase!r})")

    assert not offenders, (
        "the wording D40 removed has come back:\n  " + "\n  ".join(offenders)
    )
