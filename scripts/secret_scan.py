"""Reject real credentials before they reach a commit.

PLAN.md section 5.1: never commit real keys, add a pre-commit secret scan. Runs in
CI too, because a pre-commit hook only protects the machine that installed it.

Live Razorpay keys start `rzp_live_`; test keys start `rzp_test_`. Test keys are
still credentials and are still rejected - the difference is one underscore-
separated token, and relying on a human to notice it is how test keys end up in
public repos.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("razorpay key", re.compile(r"rzp_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "artifacts",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}

# This file necessarily contains the patterns it searches for.
SKIP_FILES = {"secret_scan.py"}

TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".toml", ".md", ".json", ".txt", ".cfg", ".ini",
    ".env", ".sh", ".ipynb", ".html", ".example", "",
}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def scan(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                rel = path.relative_to(root).as_posix()
                findings.append(f"{rel}:{line}: possible {label}: {match.group(0)[:16]}...")
    return findings


def main() -> int:
    findings = scan()
    if findings:
        print("SECRET SCAN FAILED", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("secret scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
