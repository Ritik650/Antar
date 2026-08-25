"""Execute a real Razorpay test-mode round trip and write the evidence.

    export RAZORPAY_KEY_ID=rzp_test_...
    export RAZORPAY_KEY_SECRET=...
    python tasks.py roundtrip

Track 03 asks for a system that **executes**. Everything else in this repository runs
against a simulator, and `docs/LIMITATIONS.md` L1 says so — but a reviewer opening the
repo should be able to see that the integration is real and not a description of one.
This script is the difference between *"here is how someone could run it"* and *"here is
what happened when it ran"*.

It writes `artifacts/razorpay_roundtrip.json`: one record per call, with the endpoint,
the HTTP status, the latency, the response field set, and — for failures — the exact
Razorpay error code, source and step. Enough to show the wire format was really seen.

## What it deliberately does not do

**It never sends anything to a customer.** Every call here is a fetch, an order creation,
or a payment link created with `notify.sms=false, notify.email=false` (which
`create_payment_link` forces). No charge is attempted against a mandate, because a
test-mode mandate charge needs an authenticated subscription this script has no way to
create safely, and `docs/LIMITATIONS.md` records that gap rather than papering over it.

**It refuses live keys.** A key not starting `rzp_test_` aborts before any request, the
same guard `scripts/seed_test_mode.py` uses.

**Values are redacted.** The artifact records field *names*, types and shapes — never
amounts, contact details, or ids beyond a truncated prefix. It is evidence that the call
happened and what came back, not a dump of an account.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from antar.config import artifacts_dir, secret
from antar.signals.razorpay_client import RazorpayClient, RazorpayError

REDACT_PREFIX = 8


def shape(value: Any, depth: int = 0) -> Any:
    """A structural description of a response. No values, so it is safe to commit."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"


def redact_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:REDACT_PREFIX] + "..." if len(value) > REDACT_PREFIX else value


def attempt(records: list[dict[str, Any]], name: str, fn) -> Any:
    """Run one call and record what happened, success or failure.

    A failure is evidence too - arguably better evidence, because a real 400 from
    Razorpay carries the error code, source and step that `antar/signals/taxonomy.py`
    is built to interpret, and a synthesised fixture cannot prove those field names are
    right.
    """
    started = time.perf_counter()
    record: dict[str, Any] = {"call": name}
    try:
        response = fn()
    except RazorpayError as exc:
        record.update(
            {
                "ok": False,
                "error_class": type(exc).__name__,
                "message": str(exc)[:200],
                "code": exc.payload.get("code") if hasattr(exc, "payload") else None,
                "source": exc.payload.get("source") if hasattr(exc, "payload") else None,
                "step": exc.payload.get("step") if hasattr(exc, "payload") else None,
                "reason": exc.payload.get("reason") if hasattr(exc, "payload") else None,
            }
        )
    except Exception as exc:  # evidence beats a traceback here
        record.update({"ok": False, "error_class": type(exc).__name__, "message": str(exc)[:200]})
    else:
        record.update(
            {
                "ok": True,
                "id": redact_id(response.get("id") if isinstance(response, dict) else None),
                "field_shape": shape(response),
            }
        )
        records.append(record | {"latency_ms": round((time.perf_counter() - started) * 1000)})
        return response

    records.append(record | {"latency_ms": round((time.perf_counter() - started) * 1000)})
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amount-paise", type=int, default=49900)
    args = parser.parse_args()

    key_id = secret("RAZORPAY_KEY_ID") or ""
    key_secret = secret("RAZORPAY_KEY_SECRET") or ""

    if not key_id or not key_secret:
        print(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set.\n\n"
            "This script exists to prove the integration is real, and it cannot do that "
            "without test-mode credentials. It writes nothing rather than writing an "
            "artifact that implies a round trip nobody made."
        )
        return 2

    if not key_id.startswith("rzp_test_"):
        print(
            f"refusing to run: RAZORPAY_KEY_ID starts {key_id[:9]!r}, not 'rzp_test_'. "
            "This script creates orders and payment links; it does not run against a "
            "live account."
        )
        return 1

    print(f"round trip against {key_id[:12]}... (test mode)\n", flush=True)
    records: list[dict[str, Any]] = []

    with RazorpayClient(key_id=key_id, key_secret=key_secret) as client:
        # 1. Downtime feed - read-only, and the signal L1 consumes for ISSUER_DOWN.
        attempt(records, "GET /payments/downtimes", client.fetch_downtimes)

        # 2. Create an order. The cheapest write that exercises auth, idempotency
        #    headers, and the error envelope on the way back.
        from antar.ids import idempotency_key

        order = attempt(
            records,
            "POST /orders",
            lambda: client.create_order(
                amount_paise=args.amount_paise,
                currency="INR",
                receipt="antar-roundtrip",
                key=idempotency_key("roundtrip", "order"),
            ),
        )

        # 3. Replay the same idempotency key. Razorpay should return the original rather
        #    than creating a second order - the property `PolicyGate` relies on.
        attempt(
            records,
            "POST /orders (idempotent replay)",
            lambda: client.create_order(
                amount_paise=args.amount_paise,
                currency="INR",
                receipt="antar-roundtrip",
                key=idempotency_key("roundtrip", "order"),
            ),
        )

        # 4. A payment link, with notification forced off by the executor.
        attempt(
            records,
            "POST /payment_links",
            lambda: client.create_payment_link(
                amount_paise=args.amount_paise,
                description="Antar round-trip check",
                key=idempotency_key("roundtrip", "link"),
            ),
        )

        # 5. A deliberate 400: fetch an id that cannot exist. This is the most useful
        #    record in the file, because it returns the real error envelope that
        #    antar/signals/razorpay_errors.py claims to parse.
        attempt(
            records,
            "GET /payments/{unknown} (expected failure)",
            lambda: client.fetch_payment("pay_ANTARdoesnotexist"),
        )

        if isinstance(order, dict) and order.get("id"):
            attempt(
                records,
                "GET /orders/{id}",
                lambda: client.request("GET", f"/orders/{order['id']}"),
            )

    ok = sum(1 for r in records if r.get("ok"))
    for record in records:
        mark = "ok  " if record.get("ok") else "FAIL"
        extra = record.get("id") or record.get("code") or record.get("error_class") or ""
        print(f"  [{mark}] {record['call']:44s} {record['latency_ms']:>6} ms  {extra}")

    payload = {
        "mode": "test",
        "key_id_prefix": key_id[:12] + "...",
        "calls": len(records),
        "succeeded": ok,
        "records": records,
        "note": (
            "Field names and shapes only - no amounts, no contact details, ids "
            "truncated. Evidence that the integration executed, not an account dump."
        ),
    }
    out = artifacts_dir() / "razorpay_roundtrip.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\n  {ok}/{len(records)} calls succeeded")
    print(f"  wrote {out}")

    # A failed call is a recorded outcome, not a broken script. Only a total failure -
    # nothing reached the API at all - is worth a non-zero exit.
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
