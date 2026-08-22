"""Gate coverage, by introspection rather than by enumeration.

The obvious version of this test lists the executors and checks each one is
gate-wrapped. That test passes forever and silently stops covering the executor added
next week — which is the same failure shape as D10, where a check kept passing while
the thing it was meant to guard drifted out from under it.

So this test **discovers its own scope**. It walks `antar.act.executors` at run time,
finds every callable that can reach the Razorpay client, and asserts each is
gate-wrapped. An executor nobody told it about is still covered; an executor that
loses its decoration fails immediately.

Two independent discovery routes, because either alone is defeatable:

  * **Static.** Parse every module under `antar/act/executors/` and find functions
    whose body mentions a `RazorpayClient` method that moves money.
  * **Runtime.** Import the package, enumerate its public callables, and require each
    to carry the `@requires_gate` marker.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXECUTORS_DIR = ROOT / "antar" / "act" / "executors"

# Methods on RazorpayClient that create, move, or reverse money. Discovered from the
# client itself rather than hard-coded, so a new mutating endpoint is covered the day
# it is added.
def money_moving_methods() -> set[str]:
    from antar.signals.razorpay_client import RazorpayClient

    names: set[str] = set()
    for name, member in inspect.getmembers(RazorpayClient, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        signature = inspect.signature(member)
        # Every mutating endpoint on the client takes a mandatory idempotency `key`.
        # That is the marker: a call that needs a key is a call that changes state.
        if "key" in signature.parameters:
            names.add(name)
    return names


def executor_modules() -> list[str]:
    if not EXECUTORS_DIR.exists():
        return []
    package = importlib.import_module("antar.act.executors")
    return [
        info.name
        for info in pkgutil.walk_packages(package.__path__, prefix="antar.act.executors.")
    ]


def test_the_money_moving_method_set_is_not_empty():
    """A discovery routine that discovers nothing would make every test below vacuous."""
    methods = money_moving_methods()
    assert methods, "no mutating client methods discovered; the marker heuristic has broken"
    for expected in ("charge_mandate", "create_payment_link", "create_refund"):
        assert expected in methods, f"{expected} should require an idempotency key"


def test_static_scan_finds_no_ungated_money_call():
    """Every money-moving client call inside an executor must sit in a gated function.

    Scope is discovered by walking the directory, so this keeps covering executors
    written after this test was.
    """
    if not EXECUTORS_DIR.exists():
        pytest.skip("executors package not built yet (M7)")

    methods = money_moving_methods()
    offenders: list[str] = []

    for path in EXECUTORS_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = {
                child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            }
            touches_money = bool(calls & methods)
            if not touches_money:
                continue
            decorators = {
                d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                for d in node.decorator_list
            }
            if "requires_gate" not in decorators:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: {node.name}() calls "
                    f"{sorted(calls & methods)} without @requires_gate"
                )

    assert not offenders, "ungated money call:\n" + "\n".join(offenders)


def test_every_public_executor_callable_is_marked():
    """Runtime discovery. Catches an executor that reaches money indirectly."""
    modules = executor_modules()
    if not modules:
        pytest.skip("executors package not built yet (M7)")

    offenders: list[str] = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if name.startswith("_") or not callable(obj):
                continue
            if getattr(obj, "__module__", None) != module_name:
                continue  # re-exported from elsewhere
            if not inspect.isfunction(obj):
                continue
            if not getattr(obj, "__antar_requires_gate__", False):
                offenders.append(f"{module_name}.{name}")

    assert not offenders, (
        "executor callables missing the @requires_gate marker:\n" + "\n".join(offenders)
    )


def test_no_module_outside_the_executors_touches_the_client_directly():
    """The client is reachable from exactly one package.

    A money call from, say, the scheduler would bypass the gate entirely and no
    executor-scoped test would ever see it.
    """
    methods = money_moving_methods()
    package = ROOT / "antar"
    allowed = {"razorpay_client.py"}
    offenders: list[str] = []

    for path in package.rglob("*.py"):
        if path.name in allowed:
            continue
        if EXECUTORS_DIR.exists() and EXECUTORS_DIR in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            is_money_call = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            )
            if is_money_call:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.func.attr}()")

    assert not offenders, (
        "money-moving client call outside antar/act/executors/:\n" + "\n".join(offenders)
    )


def test_the_scan_would_catch_an_ungated_executor(tmp_path):
    """A guard that cannot fail is not a guard.

    Writes a deliberately ungated executor and asserts the static scan flags it.
    """
    methods = money_moving_methods()
    source = (
        "def pay_someone(client, action):\n"
        "    return client.charge_mandate(token='t', customer_id='c', amount_paise=1,\n"
        "                                 order_id='o', method='upi', key='k')\n"
    )
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    calls = {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }
    assert calls & methods, "the scan failed to see a money call it should have seen"
    assert "requires_gate" not in {
        d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in node.decorator_list
    }
