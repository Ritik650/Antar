"""Deterministic identifiers.

PLAN.md section 9.3 requires that a fixed seed produces a byte-identical event
stream, identical features, an identical LP solution, and an identical ledger hash
chain. Random ULIDs would break that on the first line, so every id in Antar is a
pure function of its content.

The format keeps ULID's useful properties - short, URL-safe, lexicographically
sortable within a prefix - without its randomness.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

# Crockford base32: no I, L, O, U, so ids survive being read aloud on a call.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_CHARS = 20


def _b32(digest: bytes, length: int) -> str:
    value = int.from_bytes(digest, "big")
    out: list[str] = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _stringify(part: Any) -> str:
    if isinstance(part, datetime):
        return part.isoformat()
    return str(part)


def deterministic_id(prefix: str, *parts: Any) -> str:
    """A stable id derived from `parts`.

    Same inputs always give the same id, across processes and machines. Different
    inputs give a different id with overwhelming probability (100 bits).
    """
    payload = "\x1f".join(_stringify(part) for part in parts)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).digest()
    return f"{prefix}_{_b32(digest, _ID_CHARS)}"


def event_id(merchant_id: str, customer_id: str, subscription_id: str | None, cycle: Any) -> str:
    return deterministic_id("evt", merchant_id, customer_id, subscription_id, cycle)


def diagnosis_id(event_id_: str, model_version: str) -> str:
    return deterministic_id("dia", event_id_, model_version)


def decision_id(event_id_: str, policy_version: str, model_version: str) -> str:
    return deterministic_id("dec", event_id_, policy_version, model_version)


def intervention_id(event_id_: str, channel: Any, scheduled_for: Any, discount: Any) -> str:
    return deterministic_id("itv", event_id_, channel, scheduled_for, discount)


def action_id(decision_id_: str, attempt: int) -> str:
    return deterministic_id("act", decision_id_, attempt)


def idempotency_key(*parts: Any) -> str:
    """The key the PolicyGate requires on every money action.

    Derived from the decision and the attempt number so that a retried worker
    computes the same key and the replay is a provable no-op.
    """
    return deterministic_id("idem", *parts)
