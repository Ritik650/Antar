"""Replace synthesised fixtures with real captures from a test-mode account.

Run this the moment `rzp_test_` keys exist. It fetches each entity Antar consumes,
writes the raw response to `tests/fixtures/recorded/`, and compares the field set
against the synthesised fixture of the same name. Every difference is printed as a
divergence, and divergences belong in `docs/LIMITATIONS.md` - that is the entire
point of running it.

The parsing tests read whichever directory exists, preferring `recorded/`, so
swapping in real captures needs no test changes and any breakage surfaces
immediately.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from antar.config import secret
from antar.signals.razorpay_client import RazorpayClient, RazorpayError

ROOT = Path(__file__).resolve().parents[1]
SYNTHESISED = ROOT / "tests" / "fixtures" / "webhooks"
RECORDED = ROOT / "tests" / "fixtures" / "recorded"


def field_paths(value: Any, prefix: str = "") -> set[str]:
    """Flatten a payload to the set of dotted key paths it contains."""
    out: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(path)
            out |= field_paths(child, path)
    elif isinstance(value, list) and value:
        out |= field_paths(value[0], f"{prefix}[]")
    return out


def compare(recorded: dict[str, Any], synthesised: dict[str, Any]) -> tuple[set[str], set[str]]:
    real = field_paths(recorded)
    fake = field_paths(synthesised)
    return real - fake, fake - real


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payment-id", action="append", default=[])
    parser.add_argument("--subscription-id", action="append", default=[])
    args = parser.parse_args()

    key_id, key_secret = secret("RAZORPAY_KEY_ID"), secret("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        print(
            "No test-mode keys in the environment; nothing recorded.\n"
            "The synthesised fixtures in tests/fixtures/webhooks/ remain in use, and "
            "docs/LIMITATIONS.md L1 still stands."
        )
        return 0
    if not key_id.startswith("rzp_test_"):
        print("refusing to record against a live key", file=sys.stderr)
        return 2

    RECORDED.mkdir(parents=True, exist_ok=True)
    client = RazorpayClient(key_id=key_id, key_secret=key_secret)
    divergences: list[str] = []

    jobs: list[tuple[str, Any]] = [("downtimes_list", client.fetch_downtimes)]
    jobs += [(f"payment.{pid}", lambda p=pid: client.fetch_payment(p)) for pid in args.payment_id]
    jobs += [
        (f"subscription.{sid}", lambda s=sid: client.fetch_subscription(s))
        for sid in args.subscription_id
    ]

    for name, fetch in jobs:
        try:
            payload = fetch()
        except RazorpayError as exc:
            print(f"  {name}: fetch failed: {exc!r}", file=sys.stderr)
            continue

        (RECORDED / f"{name}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"  recorded {name}")

        twin = SYNTHESISED / f"{name}.json"
        if twin.exists():
            extra, missing = compare(payload, json.loads(twin.read_text(encoding="utf-8")))
            for path in sorted(extra):
                divergences.append(f"{name}: live has field not in fixture: {path}")
            for path in sorted(missing):
                divergences.append(f"{name}: fixture has field not in live: {path}")

    if divergences:
        print("\nDIVERGENCES - copy these into docs/LIMITATIONS.md:")
        for line in divergences:
            print(f"  - {line}")
    else:
        print("\nno structural divergence detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
