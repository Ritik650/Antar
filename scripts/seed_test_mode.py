"""Populate a Razorpay test-mode account with a merchant, plan, and subscriptions.

Requires `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` starting `rzp_test_`. Refuses to
run against a live key: this script creates entities, and creating entities in a live
account by accident is not a recoverable mistake.

If no keys are present it says so and exits 0 rather than failing the build. Antar's
evaluation path does not need a Razorpay account; only the live-integration path
does.
"""

from __future__ import annotations

import argparse
import sys

from antar.config import secret
from antar.ids import idempotency_key
from antar.signals.razorpay_client import RazorpayClient, RazorpayError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscriptions", type=int, default=5)
    parser.add_argument("--amount-rupees", type=int, default=499)
    args = parser.parse_args()

    key_id = secret("RAZORPAY_KEY_ID")
    key_secret = secret("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        print(
            "No RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in the environment.\n"
            "Nothing seeded. Antar's simulator, evaluation, and every test run "
            "without them; see tests/fixtures/README.md."
        )
        return 0

    if not key_id.startswith("rzp_test_"):
        print(f"refusing to seed with a non-test key ({key_id[:9]}...)", file=sys.stderr)
        return 2

    client = RazorpayClient(key_id=key_id, key_secret=key_secret)
    created: list[tuple[str, str]] = []

    try:
        plan = client.request(
            "POST",
            "/plans",
            json={
                "period": "monthly",
                "interval": 1,
                "item": {
                    "name": "Antar demo subscription",
                    "amount": args.amount_rupees * 100,
                    "currency": "INR",
                },
                "notes": {"source": "antar-seed"},
            },
            idempotency_key=idempotency_key("seed", "plan", args.amount_rupees),
        )
        created.append(("plan", str(plan.get("id"))))

        for index in range(args.subscriptions):
            customer = client.request(
                "POST",
                "/customers",
                json={
                    "name": f"Antar Demo {index + 1}",
                    "email": f"antar.demo.{index + 1}@example.com",
                    "contact": f"+9199000000{index:02d}",
                    "fail_existing": "0",
                    "notes": {"source": "antar-seed"},
                },
                idempotency_key=idempotency_key("seed", "customer", index),
            )
            created.append(("customer", str(customer.get("id"))))

            subscription = client.request(
                "POST",
                "/subscriptions",
                json={
                    "plan_id": plan["id"],
                    "customer_id": customer["id"],
                    "total_count": 12,
                    "customer_notify": 0,
                    "notes": {"source": "antar-seed", "cycle_number": "1"},
                },
                idempotency_key=idempotency_key("seed", "subscription", index),
            )
            created.append(("subscription", str(subscription.get("id"))))
    except RazorpayError as exc:
        print(f"seeding stopped: {exc!r}", file=sys.stderr)
        for kind, entity_id in created:
            print(f"  created {kind}: {entity_id}", file=sys.stderr)
        return 1

    print(f"seeded {len(created)} entities in test mode:")
    for kind, entity_id in created:
        print(f"  {kind:13s} {entity_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
