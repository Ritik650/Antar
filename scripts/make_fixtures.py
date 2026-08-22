"""Synthesise webhook fixtures in the documented Razorpay payload shape.

**Read tests/fixtures/README.md before trusting these.** PLAN.md M1 asks for
*recorded* payloads from a live test-mode account. No test-mode credentials were
available for this build, so these fixtures are synthesised from Razorpay's public
webhook documentation instead. They are structurally faithful and every error code
in them comes from `antar/signals/razorpay_errors.py`, but they were not captured
from the wire, and the repo says so everywhere rather than implying otherwise.

`scripts/record_fixtures.py` is the real-recording path; run it once keys exist and
it overwrites these files with genuine captures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from antar.signals import razorpay_errors as rz

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "webhooks"

ACCOUNT = "acc_ANTARtestacct"
BASE_TS = 1_777_000_000  # 2026-04-24T05:46:40Z, inside the simulated horizon


def envelope(event: str, contains: list[str], payload: dict[str, Any], ts: int) -> dict[str, Any]:
    return {
        "entity": "event",
        "account_id": ACCOUNT,
        "event": event,
        "contains": contains,
        "payload": payload,
        "created_at": ts,
    }


def payment_entity(
    *,
    payment_id: str,
    amount: int,
    method: str,
    bank: str,
    reason: str,
    subscription_id: str | None,
    customer_id: str,
    cycle: int,
    status: str = "failed",
) -> dict[str, Any]:
    err = rz.BY_REASON[reason]
    notes = {
        "merchant_id": "mer_antar_ott",
        "customer_id": customer_id,
        "cycle_number": str(cycle),
        "issuer": bank,
    }
    if subscription_id:
        notes["subscription_id"] = subscription_id
    return {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": status,
        "order_id": f"order_{payment_id[4:]}",
        "invoice_id": None,
        "international": False,
        "method": method,
        "amount_refunded": 0,
        "captured": status == "captured",
        "description": "Monthly subscription",
        "card_id": None,
        "bank": bank,
        "wallet": None,
        "vpa": "customer@okhdfcbank" if method == "upi" else None,
        "email": "customer@example.com",
        "contact": "+919900000000",
        "customer_id": customer_id,
        "notes": notes,
        "fee": None,
        "tax": None,
        "error_code": err.code if status == "failed" else None,
        "error_description": err.description if status == "failed" else None,
        "error_source": err.source if status == "failed" else None,
        "error_step": err.step if status == "failed" else None,
        "error_reason": err.reason if status == "failed" else None,
        "acquirer_data": {"rrn": "220134228362"},
        "created_at": BASE_TS,
    }


def subscription_entity(*, sub_id: str, status: str, paid_count: int) -> dict[str, Any]:
    return {
        "id": sub_id,
        "entity": "subscription",
        "plan_id": "plan_ANTARmonthly01",
        "customer_id": "cust_ANTAR00000001",
        "status": status,
        "current_start": BASE_TS - 30 * 86400,
        "current_end": BASE_TS,
        "charge_at": BASE_TS + 86400,
        "start_at": BASE_TS - 120 * 86400,
        "end_at": BASE_TS + 240 * 86400,
        "total_count": 12,
        "paid_count": paid_count,
        "remaining_count": 12 - paid_count,
        "quantity": 1,
        "notes": {"merchant_id": "mer_antar_ott"},
        "created_at": BASE_TS - 120 * 86400,
    }


def downtime_entity(*, did: str, status: str, severity: str, method: str, end: int | None) -> dict[str, Any]:
    return {
        "id": did,
        "entity": "payment.downtime",
        "method": method,
        "begin": BASE_TS - 3600,
        "end": end,
        "status": status,
        "scheduled": False,
        "severity": severity,
        "instrument": {"issuer": "HDFC", "psp": None, "bank": "HDFC"},
        "created_at": BASE_TS - 3600,
    }


FIXTURE_SET: dict[str, dict[str, Any]] = {
    # ---- at-risk events, one per failure class the taxonomy must resolve -----
    "payment.failed.insufficient_funds": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_INSUFFICIENTFUND",
                    amount=49900,
                    method="upi",
                    bank="HDFC",
                    reason="insufficient_funds",
                    subscription_id="sub_ANTAR00000001",
                    customer_id="cust_ANTAR00000001",
                    cycle=3,
                )
            }
        },
        BASE_TS,
    ),
    "payment.failed.issuer_down": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_ISSUERDOWN0001",
                    amount=129900,
                    method="card",
                    bank="ICICI",
                    reason="issuer_down",
                    subscription_id="sub_ANTAR00000002",
                    customer_id="cust_ANTAR00000002",
                    cycle=2,
                )
            }
        },
        BASE_TS + 60,
    ),
    "payment.failed.mandate_revoked": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_MANDATEREVOKED",
                    amount=79900,
                    method="emandate",
                    bank="SBI",
                    reason="mandate_revoked",
                    subscription_id="sub_ANTAR00000003",
                    customer_id="cust_ANTAR00000003",
                    cycle=5,
                )
            }
        },
        BASE_TS + 120,
    ),
    "payment.failed.afa_required": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_AFAREQUIRED001",
                    amount=2450000,  # Rs 24,500 - above the Rs 15,000 general ceiling
                    method="card",
                    bank="AXIS",
                    reason="additional_authentication_required",
                    subscription_id="sub_ANTAR00000004",
                    customer_id="cust_ANTAR00000004",
                    cycle=1,
                )
            }
        },
        BASE_TS + 180,
    ),
    "payment.failed.technical_decline": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_TECHNICALDECLN",
                    amount=59900,
                    method="card",
                    bank="HDFC",
                    reason="card_expired",
                    subscription_id="sub_ANTAR00000005",
                    customer_id="cust_ANTAR00000005",
                    cycle=7,
                )
            }
        },
        BASE_TS + 240,
    ),
    "payment.failed.risk_decline": envelope(
        "payment.failed",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_RISKDECLINE001",
                    amount=99900,
                    method="card",
                    bank="ICICI",
                    reason="payment_risk_check_failed",
                    subscription_id="sub_ANTAR00000006",
                    customer_id="cust_ANTAR00000006",
                    cycle=2,
                )
            }
        },
        BASE_TS + 300,
    ),
    # ---- checkout abandonment (second loss class) ---------------------------
    "invoice.expired": envelope(
        "invoice.expired",
        ["invoice"],
        {
            "invoice": {
                "entity": {
                    "id": "inv_ANTAR00000001",
                    "entity": "invoice",
                    "status": "expired",
                    "amount": 189900,
                    "currency": "INR",
                    "customer_id": "cust_ANTAR00000007",
                    "notes": {"merchant_id": "mer_antar_ott"},
                    "created_at": BASE_TS - 86400,
                }
            },
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_ABANDONED00001",
                    amount=189900,
                    method="upi",
                    bank="PAYTM",
                    reason="payment_cancelled",
                    subscription_id=None,
                    customer_id="cust_ANTAR00000007",
                    cycle=0,
                )
            },
        },
        BASE_TS + 360,
    ),
    # ---- recovery -----------------------------------------------------------
    "subscription.charged": envelope(
        "subscription.charged",
        ["subscription", "payment"],
        {
            "subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000001", status="active", paid_count=4)},
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_RECOVERED00001",
                    amount=49900,
                    method="upi",
                    bank="HDFC",
                    reason="insufficient_funds",
                    subscription_id="sub_ANTAR00000001",
                    customer_id="cust_ANTAR00000001",
                    cycle=3,
                    status="captured",
                )
            },
        },
        BASE_TS + 86400,
    ),
    "payment.captured": envelope(
        "payment.captured",
        ["payment"],
        {
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_CAPTUREDRETRY1",
                    amount=129900,
                    method="card",
                    bank="ICICI",
                    reason="issuer_down",
                    subscription_id="sub_ANTAR00000002",
                    customer_id="cust_ANTAR00000002",
                    cycle=2,
                    status="captured",
                )
            }
        },
        BASE_TS + 172800,
    ),
    "order.paid": envelope(
        "order.paid",
        ["order", "payment"],
        {
            "order": {
                "entity": {
                    "id": "order_ANTAR0000001",
                    "entity": "order",
                    "amount": 189900,
                    "amount_paid": 189900,
                    "amount_due": 0,
                    "currency": "INR",
                    "status": "paid",
                    "notes": {"merchant_id": "mer_antar_ott", "customer_id": "cust_ANTAR00000007"},
                    "created_at": BASE_TS,
                }
            },
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_LINKPAID000001",
                    amount=189900,
                    method="upi",
                    bank="PAYTM",
                    reason="payment_cancelled",
                    subscription_id=None,
                    customer_id="cust_ANTAR00000007",
                    cycle=0,
                    status="captured",
                )
            },
        },
        BASE_TS + 90000,
    ),
    # ---- mandate lifecycle --------------------------------------------------
    "subscription.halted": envelope(
        "subscription.halted",
        ["subscription"],
        {"subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000005", status="halted", paid_count=6)}},
        BASE_TS + 400,
    ),
    "subscription.cancelled": envelope(
        "subscription.cancelled",
        ["subscription"],
        {"subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000003", status="cancelled", paid_count=4)}},
        BASE_TS + 460,
    ),
    "subscription.paused": envelope(
        "subscription.paused",
        ["subscription"],
        {"subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000002", status="paused", paid_count=2)}},
        BASE_TS + 520,
    ),
    "subscription.resumed": envelope(
        "subscription.resumed",
        ["subscription"],
        {"subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000002", status="active", paid_count=2)}},
        BASE_TS + 540,
    ),
    "subscription.completed": envelope(
        "subscription.completed",
        ["subscription"],
        {"subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000008", status="completed", paid_count=12)}},
        BASE_TS + 560,
    ),
    "subscription.pending": envelope(
        "subscription.pending",
        ["subscription", "payment"],
        {
            "subscription": {"entity": subscription_entity(sub_id="sub_ANTAR00000006", status="pending", paid_count=1)},
            "payment": {
                "entity": payment_entity(
                    payment_id="pay_PENDINGCYCLE01",
                    amount=99900,
                    method="card",
                    bank="ICICI",
                    reason="declined_by_issuer",
                    subscription_id="sub_ANTAR00000006",
                    customer_id="cust_ANTAR00000006",
                    cycle=2,
                )
            },
        },
        BASE_TS + 580,
    ),
    # ---- downtime -----------------------------------------------------------
    "payment.downtime.started": envelope(
        "payment.downtime.started",
        ["payment.downtime"],
        {"payment.downtime": {"entity": downtime_entity(did="down_ANTAR0000001", status="started", severity="high", method="card", end=None)}},
        BASE_TS - 3600,
    ),
    "payment.downtime.updated": envelope(
        "payment.downtime.updated",
        ["payment.downtime"],
        {"payment.downtime": {"entity": downtime_entity(did="down_ANTAR0000001", status="started", severity="medium", method="card", end=None)}},
        BASE_TS - 1800,
    ),
    "payment.downtime.resolved": envelope(
        "payment.downtime.resolved",
        ["payment.downtime"],
        {"payment.downtime": {"entity": downtime_entity(did="down_ANTAR0000001", status="resolved", severity="medium", method="card", end=BASE_TS + 1200)}},
        BASE_TS + 1200,
    ),
}

# The Downtime API list response, for the poller.
DOWNTIME_LIST = {
    "entity": "collection",
    "count": 2,
    "items": [
        downtime_entity(did="down_ANTAR0000001", status="started", severity="high", method="card", end=None),
        downtime_entity(did="down_ANTAR0000002", status="resolved", severity="low", method="upi", end=BASE_TS - 600),
    ],
}


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, payload in FIXTURE_SET.items():
        path = FIXTURES / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written += 1
    (FIXTURES.parent / "downtimes_list.json").write_text(
        json.dumps(DOWNTIME_LIST, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {written} webhook fixtures + 1 downtime list to {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
