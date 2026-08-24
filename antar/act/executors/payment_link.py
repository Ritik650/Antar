"""Create a Razorpay payment link.

The gentlest recovery action: it takes no money by itself, it only offers a way to pay.
Still gated, because it creates a chargeable object with an amount on it, and an
unauthorised one with the wrong amount is a real problem even if nobody pays it.

## Razorpay must not send the notification

`notify` is forced off on every link. If Razorpay sent its own SMS, that message would:

  * bypass `PolicyGate` and never reach the audit ledger
  * not count against the customer's `C-BUDGET` contact allowance
  * carry Razorpay's template rather than a DLT-registered one of ours
  * arrive whenever Razorpay chose, not inside the TRAI-01 window

Four compliance holes from one convenience flag. `tests/unit/test_razorpay_client.py`
asserts the flag is off in the request body.
"""

from __future__ import annotations

from typing import Any

from antar.policy.gate import requires_gate
from antar.signals.razorpay_client import RazorpayClient
from antar.signals.schemas import ActionRecord


@requires_gate
def create_payment_link(
    action: ActionRecord,
    *,
    client: RazorpayClient,
    customer: dict[str, Any],
    description: str,
    expire_by: int | None = None,
) -> str:
    """Create a link for the amount the decision authorised. Returns the link id."""
    response = client.create_payment_link(
        # From the gate-approved record, less any discount the decision granted. A
        # discount the LLM asked for is not here, because the LLM cannot set one.
        amount_paise=max(action.amount_paise - action.discount_paise, 0),
        customer=customer,
        description=description,
        key=action.idempotency_key,
        expire_by=expire_by,
        # Antar sends its own messages, through the gate, inside the window, against a
        # registered template, counted against the contact budget.
        notify={"sms": False, "email": False},
    )
    return str(response.get("id", ""))


@requires_gate
def cancel_payment_link(action: ActionRecord, *, client: RazorpayClient, link_id: str) -> str:
    """Compensating transaction: withdraw a link created by a workflow that then failed.

    Leaving a live link behind after an aborted saga means a customer can pay against
    a decision Antar abandoned - money arriving with no matching record, which is the
    kind of reconciliation problem that takes a week to unpick.
    """
    response = client.request(
        "POST",
        f"/payment_links/{link_id}/cancel",
        json={},
        idempotency_key=action.idempotency_key + ":cancel",
    )
    return str(response.get("status", ""))


def summarise(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": response.get("id"),
        "status": response.get("status"),
        "amount": response.get("amount"),
        "short_url": response.get("short_url"),
    }
