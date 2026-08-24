"""Re-debit a mandate. **The single most dangerous call in the system.**

A retried charge that creates a second debit is the worst bug Antar could have: it
takes money from someone who already paid, and it does so through machinery they
authorised. Every defence in this file exists for that one failure.

  * The idempotency key comes from the `ActionRecord`, which derived it from the
    decision id and the attempt number. A restarted worker recomputes the same key
    rather than needing to have persisted it first, so a crash between "sent" and
    "recorded" is safe.
  * The amount comes from the `ActionRecord`, which took it from the `Decision`. Not
    from a caller argument, not from a draft, not from anything a model touched.
  * `@requires_gate` refuses a record the gate did not approve, and refuses one it
    explicitly rejected.
"""

from __future__ import annotations

from typing import Any

from antar.policy.gate import requires_gate
from antar.signals.razorpay_client import RazorpayClient, RazorpayError
from antar.signals.schemas import ActionRecord


@requires_gate
def retry_charge(
    action: ActionRecord,
    *,
    client: RazorpayClient,
    token: str,
    customer_id: str,
    order_id: str,
    method: str,
) -> str:
    """Charge a registered mandate. Returns the provider payment id.

    Raises `RazorpayError` on a genuine failure. The caller - always the gate - records
    the outcome either way, because a failed charge is as much a fact for the ledger as
    a successful one.
    """
    response = client.charge_mandate(
        token=token,
        customer_id=customer_id,
        # From the gate-approved record. There is no parameter here a caller could use
        # to charge a different amount than the one the decision authorised.
        amount_paise=action.amount_paise,
        order_id=order_id,
        method=method,
        key=action.idempotency_key,
    )
    return str(response.get("id", ""))


@requires_gate
def refund_charge(
    action: ActionRecord, *, client: RazorpayClient, payment_id: str, amount_paise: int
) -> str:
    """Compensating transaction for a charge that should not have stood.

    Used by `antar/act/saga.py` when a workflow fails partway through. Gated like every
    other money call: a refund moves real money and an unauthorised one is as much a
    problem as an unauthorised debit.

    **Never reachable from a customer message.** The adversarial suite contains an
    injection that asks for exactly this, and it dies at the gate because the LLM has
    no path to an executor at all.
    """
    response = client.create_refund(
        payment_id, amount_paise=amount_paise, key=action.idempotency_key + ":refund"
    )
    return str(response.get("id", ""))


def is_duplicate_charge(error: RazorpayError) -> bool:
    """Whether a failure means the charge already happened.

    Razorpay returns an error for a replayed idempotency key rather than silently
    succeeding. Treating that as a failure would make a restarted worker mark a
    successful charge as failed and, worse, potentially try again with a fresh key.
    """
    reason = (error.reason or "").lower()
    description = str(error.payload.get("description", "")).lower()
    return "idempot" in reason or "idempot" in description or "duplicate" in reason


def summarise(response: dict[str, Any]) -> dict[str, Any]:
    """The fields worth putting in the ledger. Deliberately not the whole payload."""
    return {
        "payment_id": response.get("id"),
        "status": response.get("status"),
        "amount": response.get("amount"),
        "method": response.get("method"),
        "error_reason": response.get("error_reason"),
    }
