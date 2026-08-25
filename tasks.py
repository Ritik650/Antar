#!/usr/bin/env python
"""Cross-platform task runner - the Makefile's twin.

`make` is not present on every machine this repo has to run on (notably the
author's Windows box), and PLAN.md N4 requires that `make evaluate` reproduces
every number from a clean checkout. Rather than make the reproducibility claim
depend on a build tool being installed, every target exists twice: once in the
Makefile for CI and Linux, once here.

    python tasks.py <target> [KEY=VALUE ...]
    python tasks.py evaluate
    python tasks.py simulate SCENARIO=base SEED=20260822

The Makefile shells out to this file, so there is exactly one implementation.
See ADR-0003.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

TARGETS: dict[str, Callable[[dict[str, str]], int]] = {}


def target(name: str) -> Callable[[Callable[[dict[str, str]], int]], Callable[..., int]]:
    def register(fn: Callable[[dict[str, str]], int]) -> Callable[..., int]:
        TARGETS[name] = fn
        return fn

    return register


def run(*cmd: str, env: dict[str, str] | None = None) -> int:
    # Force UTF-8 on child stdio. A Windows console defaults to cp1252, and a
    # single arrow in a module docstring was enough to kill `evaluate` at stage
    # zero with a UnicodeEncodeError - on the one platform this was built on, and
    # not on CI, which is the worst possible place for a difference. POSTMORTEM D30.
    env = {**os.environ, **(env or {}), "PYTHONIOENCODING": "utf-8"}

    printable = " ".join(cmd)
    print(f"\n$ {printable}", flush=True)
    merged = {**os.environ, **(env or {})}
    return subprocess.call(list(cmd), cwd=ROOT, env=merged)


def chain(*results: int) -> int:
    for code in results:
        if code != 0:
            return code
    return 0


# ---------------------------------------------------------------- quality


@target("install")
def install(_: dict[str, str]) -> int:
    return run(PY, "-m", "pip", "install", "-e", ".[dev]")


@target("lint")
def lint(_: dict[str, str]) -> int:
    return run(PY, "-m", "ruff", "check", "antar", "tests", "scripts", "tasks.py")


@target("format")
def format_(_: dict[str, str]) -> int:
    return chain(
        run(PY, "-m", "ruff", "format", "antar", "tests", "scripts", "tasks.py"),
        run(PY, "-m", "ruff", "check", "--fix", "antar", "tests", "scripts", "tasks.py"),
    )


@target("typecheck")
def typecheck(_: dict[str, str]) -> int:
    return run(PY, "-m", "mypy", "antar")


@target("test")
def test(args: dict[str, str]) -> int:
    cmd = [PY, "-m", "pytest"]
    if args.get("K"):
        cmd += ["-k", args["K"]]
    if args.get("M"):
        cmd += ["-m", args["M"]]
    return run(*cmd)


@target("coverage")
def coverage(_: dict[str, str]) -> int:
    return run(
        PY,
        "-m",
        "pytest",
        "--cov=antar",
        "--cov-report=term-missing",
        "--cov-report=xml:artifacts/coverage.xml",
        "--cov-fail-under=85",
    )


@target("check")
def check(args: dict[str, str]) -> int:
    return chain(lint(args), typecheck(args), test(args))


@target("gate")
def gate(args: dict[str, str]) -> int:
    """Everything that must be green before a commit.

    Exists because a shell chain of the form `pytest -q | tail && git commit` takes
    its exit status from `tail`, which is always 0 - so a red suite gets committed.
    That happened once. Use this target instead of composing the pipeline by hand.
    """
    return chain(lint(args), test(args))


# ---------------------------------------------------------------- pipeline


@target("simulate")
def simulate(args: dict[str, str]) -> int:
    cmd = [PY, "-m", "scripts.run_batch", "--scenario", args.get("SCENARIO", "base")]
    if args.get("SEED"):
        cmd += ["--seed", args["SEED"]]
    if args.get("POLICY"):
        cmd += ["--policy", args["POLICY"]]
    return run(*cmd)


@target("calibration-report")
def calibration_report(args: dict[str, str]) -> int:
    return run(PY, "-m", "scripts.calibration_report", "--scenario", args.get("SCENARIO", "base"))


@target("bakeoff")
def bakeoff(args: dict[str, str]) -> int:
    return run(PY, "-m", "scripts.run_bakeoff", "--scenario", args.get("SCENARIO", "base"))


@target("claims")
def claims(_: dict[str, str]) -> int:
    """Adjudicate the pre-registered claims; writes artifacts/claims.json."""
    return run(PY, "-m", "scripts.run_claims")


@target("spec-curve")
def spec_curve(args: dict[str, str]) -> int:
    return run(PY, "-m", "scripts.run_spec_curve", "--scenario", args.get("SCENARIO", "base"))


@target("trace")
def trace(args: dict[str, str]) -> int:
    """Answer 'why did you contact this customer?' for one event id."""
    cmd = [PY, "-m", "scripts.run_batch", "--scenario", args.get("SCENARIO", "base")]
    if args.get("EVENT"):
        cmd += ["--trace", args["EVENT"]]
    return run(*cmd)


@target("evaluate")
def evaluate(args: dict[str, str]) -> int:
    """The N4 target: reproduce every number in the README from a clean checkout."""
    cmd = [PY, "-m", "scripts.run_evaluation"]
    if args.get("QUICK"):
        cmd.append("--quick")
    if args.get("SCENARIOS"):
        cmd += ["--scenarios", args["SCENARIOS"]]
    return run(*cmd)


@target("figures")
def figures(_: dict[str, str]) -> int:
    return run(PY, "-m", "scripts.make_figures")


@target("register")
def register(_: dict[str, str]) -> int:
    """Regenerate docs/REGULATORY_REGISTER.md from antar/policy/regulations.py."""
    return run(PY, "-m", "scripts.make_register")


@target("console")
def console(_: dict[str, str]) -> int:
    return run(PY, "-m", "streamlit", "run", str(ROOT / "antar" / "console" / "app.py"))


@target("api")
def api(_: dict[str, str]) -> int:
    return run(PY, "-m", "uvicorn", "antar.api:app", "--host", "0.0.0.0", "--port", "8000")


@target("capture-console")
def capture_console(_: dict[str, str]) -> int:
    """Screenshot the running console into docs/img/. Start `tasks.py console` first."""
    return run(PY, "-m", "scripts.capture_console")


@target("roundtrip")
def roundtrip(_: dict[str, str]) -> int:
    """Real Razorpay test-mode round trip; writes artifacts/razorpay_roundtrip.json."""
    return run(PY, "-m", "scripts.record_roundtrip")


@target("seed-test-mode")
def seed_test_mode(_: dict[str, str]) -> int:
    return run(PY, "-m", "scripts.seed_test_mode")


@target("demo")
def demo(args: dict[str, str]) -> int:
    return chain(evaluate({**args, "QUICK": "1"}), figures(args), console(args))


@target("clean")
def clean(_: dict[str, str]) -> int:
    for path in [ROOT / "artifacts", ROOT / ".pytest_cache", ROOT / ".mypy_cache"]:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"removed {path}")
    return 0


@target("freeze")
def freeze(args: dict[str, str]) -> int:
    """Everything that must be green at code freeze (PLAN.md M8)."""
    return chain(lint(args), typecheck(args), coverage(args), evaluate(args), figures(args))


@target("help")
def help_(_: dict[str, str]) -> int:
    print("targets:")
    for name in sorted(TARGETS):
        print(f"  {name}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return help_({})
    name, rest = argv[0], argv[1:]
    kwargs = dict(item.split("=", 1) for item in rest if "=" in item)
    fn = TARGETS.get(name.replace("_", "-"))
    if fn is None:
        print(f"unknown target: {name}", file=sys.stderr)
        return help_({}) or 2
    return fn(kwargs)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
