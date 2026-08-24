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


def test_every_view_shows_the_simulated_caveat():
    """N6. A number on a dashboard travels further than the caveat under it, so the
    caveat goes on every screen rather than on the front page."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    views = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("view_")
    ]
    assert len(views) == 3, f"expected three views, found {[v.name for v in views]}"
    for view in views:
        calls = {
            node.func.id
            for node in ast.walk(view)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "simulated_banner" in calls, f"{view.name} does not state that it is simulated"


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
        f"the console reaches into the decision path: {found}. It must only read what "
        "was recorded."
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
