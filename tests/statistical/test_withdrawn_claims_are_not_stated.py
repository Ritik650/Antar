"""A claim that fails its own pre-registered test may not be stated anywhere.

`antar/eval/claims.py` says it, `docs/SIMULATOR_CARD.md` §10 says it, and until now
nothing checked it. The mechanism was documented and never built — which is the same
shape as D29, where `make evaluate` was documented to write a file it never wrote.

This is the file that makes withdrawal mechanical instead of a promise. It reads the
adjudicated verdicts from `artifacts/claims.json` and fails if a document asserts a claim
the evidence did not support.

## Why this is worth a test rather than care

Because the moment it matters is the moment nobody wants it to fire. A claim gets
withdrawn when a late correction moves a number the wrong way, which is exactly when a
deadline is closest and the prose is already written. Care is not a control.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from antar.config import artifacts_dir, repo_root

REQUIRE_ARTIFACTS = os.environ.get("ANTAR_REQUIRE_ARTIFACTS") == "1"

# Documents a reviewer reads as claims about the world. `docs/POSTMORTEM.md`,
# `docs/EVALUATION.md` and this project's ADRs are deliberately excluded: they are the
# record of what was tried and why, and a postmortem that could not mention a withdrawn
# claim could not explain how it came to be withdrawn.
ASSERTING_DOCUMENTS = (
    "README.md",
    "artifacts/RESULTS.md",
    "docs/MODEL_CARD.md",
    # The card was excluded here on the reasoning that it *defines* withdrawal, so it
    # has to be able to discuss the claim. That reasoning was wrong in the same way the
    # postmortem exclusion is right: the postmortem records what was tried, the card
    # states what is true. Excluded, it went on asserting the claim in §6.3 for three
    # review cycles after `claims.json` withdrew it. See
    # `test_the_cards_match_their_artifacts.py`, which checks the card's numbers rather
    # than its phrasings and is the guard that would actually have caught it.
    "docs/SIMULATOR_CARD.md",
)

# Phrasings that assert the sleeping-dogs population exists in reportable quantity.
# Deliberately narrow: the aim is to catch a *claim*, not every use of the words.
SLEEPING_DOGS_ASSERTIONS = (
    re.compile(r"\bwe detect sleeping dogs\b", re.I),
    re.compile(r"\bsleeping dogs (?:exist|are real|are present)\b", re.I),
    re.compile(r"\bnegative[- ]uplift (?:population|customers) (?:exist|is real)\b", re.I),
    re.compile(r"\bthe sleeping[- ]dog (?:finding|claim) (?:is |was )?(?:supported|holds)\b", re.I),
)


def verdicts() -> dict[str, dict]:
    path = artifacts_dir() / "claims.json"
    if not path.exists():
        if REQUIRE_ARTIFACTS:
            pytest.fail(
                "ANTAR_REQUIRE_ARTIFACTS=1 but artifacts/claims.json is absent. "
                "`python tasks.py evaluate` is supposed to write it (POSTMORTEM D29)."
            )
        pytest.skip("no claims.json; run `python tasks.py evaluate`")
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_claims_registry_has_a_verdict_for_sleeping_dogs():
    assert "sleeping_dogs" in verdicts()


def test_no_document_asserts_a_withdrawn_claim():
    """The control. Fires exactly when it is least convenient."""
    verdict = verdicts()["sleeping_dogs"]
    if verdict["supported"]:
        pytest.skip("the claim is currently supported; nothing to suppress")

    offences: list[str] = []
    for name in ASSERTING_DOCUMENTS:
        path = repo_root() / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in SLEEPING_DOGS_ASSERTIONS:
                if pattern.search(line):
                    offences.append(f"{name}:{number}  {line.strip()[:110]}")

    assert not offences, (
        "the sleeping-dogs claim did NOT survive its pre-registered test "
        f"({verdict['detail']}) and these lines still assert it:\n  "
        + "\n  ".join(offences)
        + "\n\nRewrite them. `docs/SIMULATOR_CARD.md` section 10 and "
        "`antar/eval/claims.py` both say a withdrawn claim may not be stated in any "
        "artifact, and this test is what makes that true rather than intended."
    )


def test_a_withdrawn_claim_is_disclosed_rather_than_omitted():
    """Silence is not the same as withdrawal.

    Deleting the claim and saying nothing would leave a reader with no way to know the
    project's motivating hypothesis was tested and failed. That is a worse outcome than
    the failure itself, and it is the tempting one.
    """
    verdict = verdicts()["sleeping_dogs"]
    if verdict["supported"]:
        pytest.skip("the claim is currently supported")

    readme = (repo_root() / "README.md").read_text(encoding="utf-8").lower()
    assert "withdrawn" in readme, (
        "the sleeping-dogs claim was withdrawn by its own pre-registered test and the "
        "README does not use the word. Report the withdrawal; do not quietly drop the "
        "claim."
    )


def test_the_registry_records_the_evidence_not_just_the_verdict():
    """A verdict with no numbers under it is an opinion."""
    evidence = verdicts()["sleeping_dogs"]["evidence"]
    assert evidence["threshold"] > 0
    assert evidence["share_by_scenario"]
    assert len(evidence["seeds"]) >= 1
