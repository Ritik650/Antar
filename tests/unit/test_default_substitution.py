"""`x or Default()` is a bug wherever `x` can be legitimately empty.

`PolicyGate.__init__` had `self.ledger = ledger or NullLedger()`. `Ledger` defines
`__len__`, so an **empty** ledger is falsy, and every caller who passed a fresh ledger
silently got a `NullLedger` instead. Every gate decision in the M8 pipeline went into a
sink and disappeared. POSTMORTEM D21.

The bug is invisible at the call site, invisible in the type signature, and appears only
on the run where the collection happens to be empty - which for an append-only ledger is
the *first* run, every time.

This test walks the AST of the package rather than grepping, and rather than listing the
classes we happen to remember. It is the ADR-0013 pattern: the check discovers what to
check.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import antar

PACKAGE = Path(antar.__file__).parent


def _defines_len_or_bool(qualname: str) -> bool:
    """Does this class (or an ancestor) make emptiness mean falsy?"""
    module_name, _, class_name = qualname.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        return False
    return any(
        "__len__" in ancestor.__dict__ or "__bool__" in ancestor.__dict__
        for ancestor in cls.__mro__
        if ancestor is not object
    )


def _candidate_defaults() -> list[tuple[str, int, str]]:
    """Every `name = name or Call()` assignment in the package.

    Returns `(file, line, callee)`. Only self-defaulting shapes are collected -
    `a = b or C()` where the left operand is the same identifier being defaulted, or an
    attribute of self with the same trailing name - because that is the parameter-default
    idiom. `x = y or z` between two unrelated values is ordinary logic.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.BoolOp):
                continue
            if not isinstance(node.value.op, ast.Or) or len(node.value.values) != 2:
                continue
            left, right = node.value.values
            if not isinstance(left, ast.Name) or not isinstance(right, ast.Call):
                continue

            target = node.targets[0]
            target_name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else target.id
                if isinstance(target, ast.Name)
                else None
            )
            if target_name != left.id:
                continue

            callee = ast.unparse(right.func)
            found.append((str(path.relative_to(PACKAGE.parent)), node.lineno, callee))
    return found


def test_no_default_substitution_on_a_class_that_can_be_empty():
    """The guard. Fails with the file and line, not with a vague complaint."""
    offenders = []
    for file, line, callee in _candidate_defaults():
        module = file.replace("\\", "/").removesuffix(".py").replace("/", ".")
        for qualname in (f"{module}.{callee}", f"antar.audit.ledger.{callee}"):
            if _defines_len_or_bool(qualname):
                offenders.append(f"{file}:{line} - `or {callee}()`")
                break

    assert not offenders, (
        "these fall back to a default when the caller passes an *empty* object, "
        "because the class defines __len__ or __bool__:\n  "
        + "\n  ".join(offenders)
        + "\nUse `X() if value is None else value`. See POSTMORTEM D21."
    )


def test_the_guard_can_actually_see_the_bug_it_was_written_for():
    """A guard nobody has watched fail is a guard nobody knows works.

    Reconstructs the exact defect - `Ledger` is falsy when empty - and asserts the
    detector recognises it. Without this, a typo in `_defines_len_or_bool` would make
    the test above pass forever.
    """
    assert _defines_len_or_bool("antar.audit.ledger.Ledger"), (
        "the detector no longer recognises the class the bug was found in"
    )
    assert not _defines_len_or_bool("antar.policy.gate.GateLimits"), (
        "the detector flags a plain dataclass, which would make it useless noise"
    )


def test_the_scan_finds_something_to_scan():
    """If the AST walk silently matched nothing, both tests above would pass vacuously."""
    assert _candidate_defaults(), "the scan found no `x or Default()` assignments at all"


def test_an_empty_ledger_survives_being_passed_to_the_gate():
    """The regression, stated as behaviour rather than as syntax."""
    from antar.audit.ledger import Ledger
    from antar.config import get_config
    from antar.policy.gate import PolicyGate

    ledger = Ledger()
    assert len(ledger) == 0
    gate = PolicyGate.from_config(get_config(), ledger=ledger)
    assert gate.ledger is ledger, "the caller's empty ledger was replaced by a default"
