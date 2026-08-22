"""Shared fixtures.

Two rules enforced here rather than trusted to discipline:

  * Every test runs on a frozen clock. A test that depends on the wall clock is a
    test that fails at 21:01 on a Tuesday, and TRAI-01's window makes that a real
    hazard in this codebase rather than a theoretical one.
  * No test touches the network. `respx` intercepts httpx; the Anthropic client is
    never constructed unless a test asks for it explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from antar import clock
from antar.config import load_config

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
RECORDED = FIXTURE_ROOT / "recorded"
SYNTHESISED = FIXTURE_ROOT / "webhooks"

# A Tuesday, 11:04 IST - inside the TRAI-01 contact window, and the timestamp the
# console's "why did you contact this customer at 11:04 on a Tuesday?" trace uses.
FROZEN_NOW = datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST)


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[clock.FrozenClock]:
    fake = clock.FrozenClock(FROZEN_NOW)
    with clock.use_clock(fake):
        yield fake


@pytest.fixture
def config():
    return load_config(environ={})


def load_fixture(name: str) -> dict[str, Any]:
    """Load a webhook fixture, preferring a real recording over a synthesised one.

    `scripts/record_fixtures.py` writes into `recorded/`. Once it has been run, every
    parsing test silently switches to the real payloads and any divergence shows up
    as a failure instead of as a wrong answer nobody noticed.
    """
    for root in (RECORDED, SYNTHESISED):
        path = root / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"no fixture named {name!r} in {RECORDED} or {SYNTHESISED}")


def all_webhook_fixtures() -> list[str]:
    root = RECORDED if RECORDED.exists() and any(RECORDED.glob("*.json")) else SYNTHESISED
    return sorted(path.stem for path in root.glob("*.json"))


@pytest.fixture
def webhook():
    return load_fixture


@pytest.fixture
def fixture_names() -> list[str]:
    return all_webhook_fixtures()
