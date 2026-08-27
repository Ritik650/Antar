"""The console reads artifacts by key. Those keys must exist.

`antar/console/app.py` cannot be imported in a test — Streamlit executes the page on
import — so nothing would otherwise notice if a renamed artifact field turned a panel
into a `KeyError` at demo time. That is a bad moment to find out.

So this file does two things without importing the app: it compiles the module (a syntax
error in a file nothing imports is invisible until someone runs it), and it extracts the
artifact keys the source subscripts, then checks them against the real artifacts.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import antar
from antar.config import artifacts_dir

APP = Path(antar.__file__).parent / "console" / "app.py"


def test_the_console_module_compiles():
    """It is never imported, so nothing else would tell us."""
    compile(APP.read_text(encoding="utf-8"), str(APP), "exec")


def test_the_console_declares_the_three_views_the_plan_specifies():
    source = APP.read_text(encoding="utf-8")
    for view in ("Batch overview", "Decision trace", "Shadow prices"):
        assert view in source, f"PLAN.md M9 names the {view!r} view; the console has none"


def _functions(prefix: str) -> list[ast.FunctionDef]:
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith(prefix)
    ]


def _calls(node: ast.FunctionDef) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def test_the_plan_specified_views_are_still_exactly_three():
    """The console grew a walkthrough. M9's acceptance criterion did not change.

    Walkthrough screens are `screen_*`; the three operator views PLAN.md specifies are
    `view_*` and remain a closed set, so adding demo surface cannot quietly redefine
    what the milestone delivered.
    """
    views = _functions("view_")
    assert len(views) == 3, f"expected three views, found {[v.name for v in views]}"


def test_every_screen_shows_the_simulated_caveat():
    """N6. A number on a dashboard travels further than the caveat under it, so the
    caveat goes on every screen rather than on the front page.

    Applies to walkthrough screens as well as operator views: the walkthrough is what a
    reviewer actually watches, so it is the surface where an unlabelled simulated number
    would do the damage. Shared panels deliberately carry no banner - the screen that
    composes them does, which is what keeps one caveat on screen instead of three.
    """
    screens = _functions("view_") + _functions("screen_")
    assert len(screens) > 3, "the walkthrough screens are missing"
    for screen in screens:
        assert "simulated_banner" in _calls(screen), (
            f"{screen.name} does not state that it is simulated"
        )


def test_every_ledger_backed_view_checks_the_chain():
    """A trace from a ledger that does not verify is worse than no trace."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for name in ("view_batch_overview", "view_trace_explorer"):
        view = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        calls = {
            node.func.id
            for node in ast.walk(view)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "chain_banner" in calls, f"{name} shows ledger data without verifying it"


def _subscripted_string_keys(source: str, on_variable: str) -> set[str]:
    """String keys the source reads off `<on_variable>[...]`.

    A blunt instrument on purpose: it finds `summary["events"]` and ignores everything
    dynamic. It cannot prove the console is correct, only that it does not name a field
    that no longer exists - which is the failure that actually happens.
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == on_variable
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


def _artifact(name: str) -> dict:
    path = artifacts_dir() / name
    if not path.exists():
        pytest.skip(f"{name} not present; run `python tasks.py evaluate`")
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_batch_keys_the_console_reads_all_exist():
    data = _artifact("batch_base.json")
    source = APP.read_text(encoding="utf-8")
    missing = sorted(_subscripted_string_keys(source, "summary") - set(data))
    assert not missing, (
        f"the console reads {missing} from the batch artifact, which does not have "
        "them. This is a KeyError at demo time."
    )


def test_the_allocation_keys_the_console_reads_all_exist():
    data = _artifact("allocation_base.json")
    source = APP.read_text(encoding="utf-8")
    read = _subscripted_string_keys(source, "data")
    # `data` is also the allocation artifact in view_shadow_prices; the batch artifact
    # is bound to `summary`. Keys read off `data` must therefore be allocation keys.
    missing = sorted(read - set(data))
    assert not missing, f"the console reads {missing} from the allocation artifact"


def test_the_console_never_recomputes_a_decision():
    """The architectural claim, enforced.

    A console that recomputes is a second implementation of the decision path, and when
    the two disagree the operator cannot tell which is lying. So the console may read
    the ledger and the artifacts, and may not call the allocator, the learners, the
    simulator, or the pipeline.
    """
    source = APP.read_text(encoding="utf-8")
    forbidden = [
        "run_pipeline",
        "from antar.decide",
        "from antar.simulator",
        "import solve",
        "predict_uplift",
        "generate(",
    ]
    found = [name for name in forbidden if name in source]
    assert not found, (
        f"the console reaches into the decision path: {found}. It must only read what was recorded."
    )


# ------------------------------------------------------- sidebar dispatch


def _section_lists() -> dict[str, list[str]]:
    """The sidebar's section names, read out of the source without importing it."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    lists: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"WALKTHROUGH", "OPERATOR", "REFERENCE"}
        ):
            lists[node.targets[0].id] = ast.literal_eval(node.value)
    return lists


def test_every_sidebar_section_has_a_branch_that_renders_it():
    """A section in the list with no branch in `render` is a blank page.

    It is invisible to every other test here - the module still compiles, every
    artifact key still exists - and it surfaces as an empty screen at the exact
    moment someone clicks it on camera. So the dispatch is checked against the list
    that drives the sidebar, in both directions.
    """
    lists = _section_lists()
    assert set(lists) == {"WALKTHROUGH", "OPERATOR", "REFERENCE"}, (
        f"expected three section lists, found {sorted(lists)}"
    )
    sections = [name for group in lists.values() for name in group]
    assert len(sections) == len(set(sections)), "two sections share a name"

    tree = ast.parse(APP.read_text(encoding="utf-8"))
    render = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render"
    )
    compared = {
        node.comparators[0].value
        for node in ast.walk(render)
        if isinstance(node, ast.Compare)
        and isinstance(node.ops[0], ast.Eq)
        and isinstance(node.comparators[0], ast.Constant)
        and isinstance(node.comparators[0].value, str)
    }

    unrendered = sorted(set(sections) - compared)
    assert not unrendered, f"the sidebar offers {unrendered}, which `render` never draws"

    orphaned = sorted(compared - set(sections))
    assert not orphaned, f"`render` has a branch for {orphaned}, which the sidebar never offers"


def test_the_ledger_backed_sections_are_named_in_the_guard():
    """`render` asserts a ledger is present on some branches. The guard must agree.

    If a ledger-reading section is left out of `NEEDS_LEDGER`, the assertion fires as an
    `AssertionError` traceback on screen instead of the sentence explaining that a
    ledger has to be selected.
    """
    source = APP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "NEEDS_LEDGER"
    )

    render = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render"
    )
    # A branch that asserts the ledger is not None is a branch that needs one.
    asserting: set[str] = set()
    for branch in ast.walk(render):
        if not isinstance(branch, ast.If):
            continue
        if not (
            isinstance(branch.test, ast.Compare)
            and isinstance(branch.test.comparators[0], ast.Constant)
            and isinstance(branch.test.comparators[0].value, str)
        ):
            continue
        if any(isinstance(node, ast.Assert) for node in branch.body):
            asserting.add(branch.test.comparators[0].value)

    missing_guard = sorted(asserting - set(guarded))
    assert not missing_guard, (
        f"{missing_guard} assert a ledger in `render` but are not in NEEDS_LEDGER, so "
        "selecting them without a ledger raises instead of explaining"
    )


# ----------------------------------------------- the architecture diagram


def test_the_diagram_matches_the_package():
    """Every box names a path that exists.

    A diagram is a claim about the code, and a diagram drawn in a separate tool drifts
    from it silently. This one is drawn in Python, so the claim is checkable.
    """
    from antar.config import repo_root
    from scripts.make_architecture_diagram import LAYERS

    for _title, module, _job, _colour in LAYERS:
        assert (repo_root() / module).exists(), f"the diagram names {module}, which does not exist"


def test_the_diagram_covers_every_layer_the_architecture_doc_describes():
    from antar.config import repo_root
    from scripts.make_architecture_diagram import LAYERS

    titles = {title.split()[0] for title, *_ in LAYERS}
    assert titles == {"L1", "L2", "L3", "PolicyGate", "L4", "L5"}

    architecture = (repo_root() / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for _title, module, _job, _colour in LAYERS:
        assert module in architecture, f"ARCHITECTURE.md does not mention {module}"


# --------------------------------------------- links that cannot be followed


def test_the_console_does_not_present_unresolvable_links_as_links():
    """A drafted message contains `https://pay.antar.example/...`, which cannot resolve.

    `.example` is reserved by RFC 2606 so that it never exists, and the `url` slot
    validator in `act/templates/` accepts no other host — a drafted message is
    structurally incapable of carrying a link that reaches anything. That is a safety
    property, not a limitation.

    But Streamlit auto-links anything URL-shaped, so a reader clicks one, gets
    `DNS_PROBE_FINISHED_NXDOMAIN`, and reasonably concludes the system is broken. In a
    demo that is a self-inflicted wound. The narrative is rendered through
    `defuse_links`, which wraps URLs in backticks so they display as text.
    """
    source = APP.read_text(encoding="utf-8")
    assert "defuse_links(trace.narrate())" in source, (
        "the trace narrative is rendered without defusing its URLs; a viewer will click "
        "an unresolvable link and read the DNS failure as a defect"
    )
    assert "RFC 2606" in source, (
        "the console should say why the links cannot resolve, not merely hide them"
    )


def test_defusing_preserves_the_sentence_and_wraps_every_url():
    """Presentation only. The exact string still appears in the raw ledger entries."""
    import re as _re

    source = APP.read_text(encoding="utf-8")
    segment = source[source.index("URL = re.compile") : source.index("def simulated_banner")]
    namespace: dict = {"re": _re}
    exec(compile(segment, "console", "exec"), namespace, namespace)
    defuse = namespace["defuse_links"]

    text = (
        "You can complete it here: https://pay.antar.example/ABC123. This link expires "
        "on 2 Apr 2026. To cancel, visit https://link.antar.example/ABC123/update."
    )
    out = defuse(text)

    assert "`https://pay.antar.example/ABC123`." in out
    assert "`https://link.antar.example/ABC123/update`." in out
    # The full stops must stay outside the code spans, or the prose reads as a URL.
    assert out.count("`") == 4
    assert "2 Apr 2026" in out, "non-URL text must be untouched"
