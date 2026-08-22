"""How a true failure cause surfaces as a Razorpay error payload.

This module is deliberately kept separate from `antar/detect/taxonomy.py`, and the
two were written independently. If the generator emitted error codes by inverting the
detector's lookup table, detection would score perfectly and prove nothing - a
circularity as damaging as a planted treatment effect.

Instead, the emission distributions overlap the way real payment errors overlap:

  * `gateway_technical_error` is emitted by both `ISSUER_DOWN` and
    `TECHNICAL_DECLINE`. The error string alone cannot separate them; only the
    downtime cross-check and the segment changepoint can.
  * `declined_by_issuer` is emitted by both `INSUFFICIENT_FUNDS` and
    `RISK_DECLINE`, because banks routinely decline for funds without saying so.
  * `payment_failed` is emitted by everything, because a residual bucket always is.

That overlap is what forces `antar/detect/` to do real work, and it is also why the
`UNKNOWN` rate is non-zero and reported rather than hidden.

**The shares below are invented.** The labels and payload structure are documented.
docs/SIMULATOR_CARD.md section 4.2 and 12.5 both say so.
"""

from __future__ import annotations

import numpy as np

from antar.signals.razorpay_errors import BY_REASON, ErrorCode
from antar.signals.schemas import FailureClass

EMISSION: dict[FailureClass, dict[str, float]] = {
    FailureClass.ISSUER_DOWN: {
        "issuer_down": 0.40,
        "gateway_technical_error": 0.22,
        "payment_timed_out": 0.14,
        "server_error": 0.10,
        "network_error": 0.08,
        "payment_failed": 0.06,
    },
    FailureClass.INSUFFICIENT_FUNDS: {
        "insufficient_funds": 0.78,
        "payment_limit_exceeded": 0.07,
        "max_amount_exceeded": 0.04,
        "declined_by_issuer": 0.08,
        "payment_failed": 0.03,
    },
    FailureClass.TECHNICAL_DECLINE: {
        "card_expired": 0.14,
        "card_blocked": 0.09,
        "invalid_vpa": 0.08,
        "account_does_not_exist": 0.08,
        "invalid_account": 0.08,
        "account_frozen": 0.06,
        "incorrect_otp": 0.07,
        "authentication_failed": 0.07,
        "gateway_technical_error": 0.11,
        # Timeouts, network errors and 5xx are not the exclusive property of an
        # outage. A broken instrument produces them too - a card that the issuer's
        # tokenisation service cannot resolve times out exactly like a bank that is
        # down. Emitting them only from ISSUER_DOWN made them a perfect tell and
        # handed the classifier a 98% recall it had not earned. POSTMORTEM D9.
        "payment_timed_out": 0.07,
        "network_error": 0.05,
        "server_error": 0.04,
        "payment_failed": 0.06,
    },
    FailureClass.RISK_DECLINE: {
        "payment_risk_check_failed": 0.38,
        "risk_threshold_exceeded": 0.31,
        "declined_by_issuer": 0.23,
        "payment_failed": 0.08,
    },
    FailureClass.AFA_REQUIRED: {
        "additional_authentication_required": 0.68,
        "authentication_failed": 0.19,
        "incorrect_otp": 0.13,
    },
    FailureClass.MANDATE_REVOKED: {
        "mandate_revoked": 0.44,
        "subscription_cancelled": 0.29,
        "invalid_mandate": 0.21,
        "payment_cancelled": 0.06,
    },
}


def _validate() -> None:
    for failure_class, table in EMISSION.items():
        total = sum(table.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"{failure_class} emission shares sum to {total}, not 1.0")
        for reason in table:
            if reason not in BY_REASON:
                raise ValueError(
                    f"{failure_class} emits {reason!r}, which is not in the documented "
                    "Razorpay taxonomy"
                )


_validate()

_REASONS: dict[FailureClass, tuple[str, ...]] = {
    cls: tuple(table) for cls, table in EMISSION.items()
}
_WEIGHTS: dict[FailureClass, np.ndarray] = {
    cls: np.array(list(table.values()), dtype=float) for cls, table in EMISSION.items()
}


def emit(failure_class: FailureClass, rng: np.random.Generator) -> ErrorCode:
    """Draw the error payload a failure of this class surfaces as."""
    table = _REASONS.get(failure_class)
    if table is None:
        return BY_REASON["payment_failed"]
    reason = str(rng.choice(table, p=_WEIGHTS[failure_class]))
    return BY_REASON[reason]


def ambiguous_reasons() -> set[str]:
    """Reasons emitted by more than one true class.

    Reported by the calibration report, because the size of this set is a direct
    lower bound on how much work the classifier and the downtime cross-check have to
    do - and therefore on how much of the detection result is genuine.
    """
    seen: dict[str, int] = {}
    for table in EMISSION.values():
        for reason in table:
            seen[reason] = seen.get(reason, 0) + 1
    return {reason for reason, count in seen.items() if count > 1}


def emission_frame() -> list[dict[str, object]]:
    """Long-format view for the calibration report."""
    return [
        {"failure_class": cls.value, "error_reason": reason, "share": share}
        for cls, table in EMISSION.items()
        for reason, share in table.items()
    ]
