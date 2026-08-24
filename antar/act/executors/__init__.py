"""L4 executors. **Every callable here is reachable only through `PolicyGate`.**

N2: every money action passes through the gate and appears in the audit ledger. No
exceptions, no bypass path, not even in tests - tests use a `dry_run` gate, never a
disabled one.

Two mechanisms enforce that, and neither is a convention:

  * `@requires_gate` raises at run time if an executor is called without an
    `ActionRecord` the gate has approved.
  * `tests/unit/test_gate_coverage.py` **discovers its own scope**: it derives the set
    of money-moving client methods by introspecting `RazorpayClient` for calls that
    require an idempotency key, walks this package at run time, and asserts every
    function reaching one of them carries the decorator. An executor nobody told the
    test about is still covered.

The second is the one that matters. A test that enumerated these three executors by
hand would pass forever and quietly stop covering the fourth - the same failure shape
as POSTMORTEM D10, where a check kept passing while what it guarded moved out from
under it.
"""

from antar.act.executors.notify import send_notification
from antar.act.executors.payment_link import create_payment_link
from antar.act.executors.retry_charge import retry_charge

__all__ = ["create_payment_link", "retry_charge", "send_notification"]
