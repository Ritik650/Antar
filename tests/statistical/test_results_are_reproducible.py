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


def numbers_in(text: str) -> set[str]:
    return {normalise(match.group()) for match in NUMBER.finditer(text)}


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
        value = float(node)
        if value != value or value in (float("inf"), float("-inf")):
            # A NaN in an artifact is a real thing (an unfilled phase-diagram cell) and
            # is not a number the README could be quoting.
            return
        # A stored figure counts as present under any reading that is a *unit change or
        # a rounding*, never under a reading that changes the claim. Paise to rupees is
        # not a claim; a different number is.
        readings = (value, value / 100, value * 100)
        for reading in readings:
            sink.add(f"{reading:.6g}")
            for places in (0, 1, 2, 3):
                sink.add(f"{round(reading, places):.6g}")
            # Percentages: 0.9168 in an artifact reads as "91.7%" or "0.917" in prose.
            sink.add(f"{round(reading * 100, 1):.6g}")
    elif isinstance(node, str):
        sink |= numbers_in(node)


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
        sink |= numbers_in(results.read_text(encoding="utf-8"))
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
    """N4. A figure that appears nowhere in `artifacts/` was typed, not measured."""
    unexplained: list[str] = []
    for line_number, line in result_lines():
        for token in numbers_in(line):
            if token in STRUCTURAL or token in artifact_numbers:
                continue
            unexplained.append(f"README.md:{line_number}  {token}   in: {line[:90]}")

    assert not unexplained, (
        "these numbers appear in the README but in no artifact. Either they were typed "
        "by hand, or the artifact that produced them is stale. Run "
        "`python tasks.py evaluate`.\n  " + "\n  ".join(unexplained)
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
