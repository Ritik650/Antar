"""The documented Razorpay error taxonomy.

Provenance: compiled from Razorpay's public payment-error documentation in August
2026. These are payload *facts* - the shape and vocabulary of `error_code`,
`error_reason`, `error_source`, `error_step`, `error_description` - not
frequencies. SIMULATOR_CARD.md section 4.2 is explicit about the distinction: the
labels and payload structure are real, the shares are invented.

Verification duty (mirrors PLAN.md section 3): before the final commit the owner
re-checks this catalogue against the live documentation. Any entry that cannot be
confirmed is removed rather than guessed at, because a fabricated error code in a
fixture is indistinguishable from a real one to a reviewer and that is exactly the
kind of thing that destroys trust in everything else in the repo.

`tests/statistical/test_taxonomy_realism.py` asserts that every error code the
simulator emits appears here.
"""

from __future__ import annotations

from typing import NamedTuple


class ErrorCode(NamedTuple):
    """One row of the documented taxonomy."""

    code: str  # error.code       - the coarse bucket
    reason: str  # error.reason     - the machine-readable cause
    source: str  # error.source     - who failed
    step: str  # error.step       - where in the flow
    description: str  # error.description - customer-facing text


# error.code buckets, as documented.
BAD_REQUEST = "BAD_REQUEST_ERROR"
GATEWAY_ERROR = "GATEWAY_ERROR"
SERVER_ERROR = "SERVER_ERROR"

# error.source values, as documented.
SOURCE_CUSTOMER = "customer"
SOURCE_BUSINESS = "business"
SOURCE_BANK = "bank"
SOURCE_ISSUER = "issuer"
SOURCE_GATEWAY = "gateway"
SOURCE_NETWORK = "network"
SOURCE_INTERNAL = "internal"

# error.step values, as documented.
STEP_INITIATION = "payment_initiation"
STEP_AUTHENTICATION = "payment_authentication"
STEP_AUTHORIZATION = "payment_authorization"
STEP_CAPTURE = "payment_capture"
STEP_RESPONSE = "payment_response"


CATALOGUE: tuple[ErrorCode, ...] = (
    # ---- funds -----------------------------------------------------------
    ErrorCode(
        BAD_REQUEST,
        "insufficient_funds",
        SOURCE_CUSTOMER,
        STEP_AUTHORIZATION,
        "Your payment could not be completed due to insufficient funds in the account.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "payment_limit_exceeded",
        SOURCE_CUSTOMER,
        STEP_AUTHORIZATION,
        "The transaction amount exceeds the limit set on the account.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "max_amount_exceeded",
        SOURCE_CUSTOMER,
        STEP_AUTHORIZATION,
        "The amount is higher than the maximum permitted for this instrument.",
    ),
    # ---- issuer / bank availability --------------------------------------
    ErrorCode(
        GATEWAY_ERROR,
        "issuer_down",
        SOURCE_ISSUER,
        STEP_AUTHORIZATION,
        "The bank is currently unavailable. Please retry after some time.",
    ),
    ErrorCode(
        GATEWAY_ERROR,
        "gateway_technical_error",
        SOURCE_GATEWAY,
        STEP_AUTHORIZATION,
        "A technical error occurred at the payment gateway.",
    ),
    ErrorCode(
        GATEWAY_ERROR,
        "server_error",
        SOURCE_BANK,
        STEP_AUTHORIZATION,
        "The bank server returned an error while processing the payment.",
    ),
    ErrorCode(
        GATEWAY_ERROR,
        "payment_timed_out",
        SOURCE_NETWORK,
        STEP_AUTHORIZATION,
        "The payment request timed out before the bank responded.",
    ),
    ErrorCode(
        GATEWAY_ERROR,
        "network_error",
        SOURCE_NETWORK,
        STEP_RESPONSE,
        "A network error interrupted the payment.",
    ),
    # ---- instrument / technical decline ----------------------------------
    ErrorCode(
        BAD_REQUEST,
        "card_expired",
        SOURCE_CUSTOMER,
        STEP_AUTHORIZATION,
        "The card used for the payment has expired.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "card_blocked",
        SOURCE_ISSUER,
        STEP_AUTHORIZATION,
        "The card has been blocked by the issuing bank.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "invalid_vpa",
        SOURCE_CUSTOMER,
        STEP_INITIATION,
        "The UPI ID entered is invalid.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "account_does_not_exist",
        SOURCE_CUSTOMER,
        STEP_AUTHORIZATION,
        "The bank account associated with the mandate could not be found.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "invalid_account",
        SOURCE_BANK,
        STEP_AUTHORIZATION,
        "The account details registered against this mandate are invalid.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "account_frozen",
        SOURCE_ISSUER,
        STEP_AUTHORIZATION,
        "The account has been frozen by the bank and cannot be debited.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "incorrect_otp",
        SOURCE_CUSTOMER,
        STEP_AUTHENTICATION,
        "The one-time password entered was incorrect.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "authentication_failed",
        SOURCE_CUSTOMER,
        STEP_AUTHENTICATION,
        "Payment authentication could not be completed.",
    ),
    # ---- risk ------------------------------------------------------------
    ErrorCode(
        BAD_REQUEST,
        "payment_risk_check_failed",
        SOURCE_BUSINESS,
        STEP_AUTHORIZATION,
        "The payment was declined by a risk check.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "risk_threshold_exceeded",
        SOURCE_ISSUER,
        STEP_AUTHORIZATION,
        "The issuer declined the payment on risk grounds.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "declined_by_issuer",
        SOURCE_ISSUER,
        STEP_AUTHORIZATION,
        "The issuing bank declined the transaction.",
    ),
    # ---- mandate lifecycle ------------------------------------------------
    ErrorCode(
        BAD_REQUEST,
        "invalid_mandate",
        SOURCE_BUSINESS,
        STEP_INITIATION,
        "The mandate is no longer valid for this debit.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "mandate_revoked",
        SOURCE_CUSTOMER,
        STEP_INITIATION,
        "The customer has revoked the mandate.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "subscription_cancelled",
        SOURCE_CUSTOMER,
        STEP_INITIATION,
        "The subscription associated with this payment has been cancelled.",
    ),
    ErrorCode(
        BAD_REQUEST,
        "payment_cancelled",
        SOURCE_CUSTOMER,
        STEP_AUTHENTICATION,
        "The customer cancelled the payment.",
    ),
    # ---- additional factor of authentication -----------------------------
    ErrorCode(
        BAD_REQUEST,
        "additional_authentication_required",
        SOURCE_BANK,
        STEP_AUTHENTICATION,
        "This amount requires additional factor authentication by the customer.",
    ),
    # ---- residual --------------------------------------------------------
    ErrorCode(
        SERVER_ERROR,
        "payment_failed",
        SOURCE_INTERNAL,
        STEP_RESPONSE,
        "The payment could not be completed.",
    ),
)


BY_REASON: dict[str, ErrorCode] = {entry.reason: entry for entry in CATALOGUE}

DOCUMENTED_REASONS: frozenset[str] = frozenset(BY_REASON)
DOCUMENTED_CODES: frozenset[str] = frozenset(entry.code for entry in CATALOGUE)
DOCUMENTED_SOURCES: frozenset[str] = frozenset(entry.source for entry in CATALOGUE)
DOCUMENTED_STEPS: frozenset[str] = frozenset(entry.step for entry in CATALOGUE)


def lookup(reason: str) -> ErrorCode | None:
    return BY_REASON.get(reason)


def is_documented(reason: str) -> bool:
    return reason in BY_REASON
