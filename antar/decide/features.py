"""Feature construction for the uplift models. **No LLM, no latents.**

This is the file `docs/SIMULATOR_CARD.md` §10 singles out:

> If any of them leaks into `antar/decide/features.py` - even indirectly, through a
> derived column - the uplift models will look spectacular and mean nothing.

Two defences, because the static one was demonstrably insufficient (POSTMORTEM D10, a
leak that travelled by function argument and passed every import check):

  * `FEATURE_COLUMNS` is an explicit allowlist. Nothing reaches a model that is not
    named here.
  * `assert_no_canary` runs on the constructed frame. Every latent bundle carries a
    per-customer signature; if any value was copied out of the answer key it arrives
    with the marker attached, whatever route it took.

## What the features are

Everything a real merchant could compute from their own records plus Razorpay's public
feeds. Deliberately *not* a large set - the honest constraint is that a merchant knows
the mandate, the failure, the segment, and their own contact history, and very little
else about a customer who has never replied to them.

`tenure_months` is the one item that also appears in the latent vector. It is
observable by design - a merchant reads it from their own subscription table - and is
covered by the single-entry allowlist in `tests/statistical/test_no_leakage.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from antar.signals.schemas import (
    HIGH_CEILING_CATEGORIES,
    AtRiskEvent,
    CustomerContext,
    Diagnosis,
    FailureClass,
    InterventionClass,
    SegmentHealth,
)

CATEGORICAL: tuple[str, ...] = (
    "failure_class",
    "recommended_class",
    "method",
    "issuer",
    "merchant_category",
    "segment_health",
    "loss_class",
)

NUMERIC: tuple[str, ...] = (
    "log_amount",
    "above_afa_ceiling",
    "cycle_number",
    "hour_of_day",
    "day_of_month",
    "days_to_month_end",
    "tenure_months",
    "contacts_in_window",
    "hours_since_last_contact",
    "prior_attempts",
    "diagnosis_confidence",
    "downtime_overlap",
    "afa_required",
    "is_checkout_abandon",
)

FEATURE_COLUMNS: tuple[str, ...] = CATEGORICAL + NUMERIC

# Anything a model may never see, listed by name so the intent is explicit rather than
# implied by absence. Asserted by `test_forbidden_columns_are_absent`.
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "p_self_heal_base",
        "optout_sensitivity",
        "persuadability",
        "price_sensitivity",
        "intent_to_churn",
        "salary_day",
        "balance_half_life_days",
        "canary",
        "true_failure_class",
        "uplift",
        "recovered",
        "outcome",
    }
)

NO_CONTACT_SENTINEL = 1e6
"""Hours since last contact, when there has never been one. A large finite number
rather than NaN, so tree splits behave predictably and the column stays float."""


@dataclass(frozen=True)
class FeatureRow:
    event_id: str
    customer_id: str
    values: dict[str, Any]


def build_row(
    event: AtRiskEvent,
    customer: CustomerContext,
    diagnosis: Diagnosis | None = None,
    *,
    now: Any = None,
    prior_attempts: int = 0,
) -> FeatureRow:
    """One feature row. A pure function of observable state.

    `now` is required for the contact-recency feature and is passed explicitly rather
    than read from the clock, because this feeds an inferential pipeline - see
    docs/CLOCK_AUDIT.md and POSTMORTEM D13.
    """
    reference = now if now is not None else event.occurred_at
    ceiling = (
        10_000_000 if event.merchant_category in HIGH_CEILING_CATEGORIES else 1_500_000
    )

    if customer.last_contact_at is None:
        hours_since_contact = NO_CONTACT_SENTINEL
    else:
        hours_since_contact = (reference - customer.last_contact_at).total_seconds() / 3600.0

    # Days to month end: a proxy for where in the salary cycle we are, derived from the
    # calendar rather than from the customer's hidden balance curve.
    if event.occurred_at.month == 12:
        month_end_day = 31
    else:
        from calendar import monthrange

        month_end_day = monthrange(event.occurred_at.year, event.occurred_at.month)[1]

    values: dict[str, Any] = {
        "failure_class": (diagnosis.failure_class if diagnosis else FailureClass.UNKNOWN).value,
        "recommended_class": (
            diagnosis.recommended_class if diagnosis else InterventionClass.WAIT
        ).value,
        "method": event.method.value,
        "issuer": event.issuer,
        "merchant_category": event.merchant_category.value,
        "segment_health": (diagnosis.segment_health if diagnosis else SegmentHealth.HEALTHY).value,
        "loss_class": event.loss_class.value,
        "log_amount": float(np.log1p(event.amount_paise)),
        "above_afa_ceiling": float(event.amount_paise > ceiling),
        "cycle_number": float(event.cycle_number or 0),
        "hour_of_day": float(event.occurred_at.hour),
        "day_of_month": float(event.occurred_at.day),
        "days_to_month_end": float(month_end_day - event.occurred_at.day),
        "tenure_months": float(customer.tenure_months),
        "contacts_in_window": float(customer.contacts_in_window),
        "hours_since_last_contact": float(hours_since_contact),
        "prior_attempts": float(prior_attempts),
        "diagnosis_confidence": float(diagnosis.confidence) if diagnosis else 0.0,
        "downtime_overlap": float(bool(diagnosis and diagnosis.downtime_overlap)),
        "afa_required": float(bool(diagnosis and diagnosis.afa_required)),
        "is_checkout_abandon": float(event.loss_class.value == "CHECKOUT_ABANDON"),
    }
    return FeatureRow(event_id=event.event_id, customer_id=customer.customer_id, values=values)


def build_frame(rows: Sequence[FeatureRow], *, check_canary: bool = True) -> pd.DataFrame:
    """Assemble rows into the matrix a learner sees.

    `check_canary` is on by default and should stay on. It is the data-flow half of the
    leakage defence, and it exists because the import-graph half passed cleanly while
    `antar/detect/pipeline.py` was reading ground truth through a function argument.
    """
    frame = pd.DataFrame([row.values for row in rows], columns=list(FEATURE_COLUMNS))
    for column in CATEGORICAL:
        frame[column] = frame[column].astype("category")

    if check_canary:
        assert_no_canary(frame)

    present = set(frame.columns)
    leaked = present & FORBIDDEN_COLUMNS
    if leaked:
        raise ValueError(f"forbidden columns reached the feature frame: {sorted(leaked)}")
    return frame


def assert_no_canary(frame: pd.DataFrame) -> None:
    """Fail if an answer-key marker is reachable from the feature matrix.

    The marker is defined in `antar/simulator/latents.py`, which this module must not
    import - so the prefix is duplicated here as a literal. That duplication is
    deliberate: a production layer importing the simulator is exactly the leak this
    guards against, and `test_no_leakage` enforces the no-import rule separately.
    """
    marker = "ANTARCANARY"
    for column in frame.columns:
        if frame[column].dtype.name in ("category", "object"):
            values = frame[column].astype(str)
            if values.str.contains(marker, regex=False).any():
                raise ValueError(
                    f"answer-key canary found in feature column {column!r}. Something "
                    "copied a value out of CustomerLatents into data a model consumes. "
                    "See docs/POSTMORTEM.md D10."
                )


def feature_summary(frame: pd.DataFrame) -> dict[str, Any]:
    """What went into the models, for the model card."""
    return {
        "n_rows": len(frame),
        "n_features": int(frame.shape[1]),
        "categorical": list(CATEGORICAL),
        "numeric": list(NUMERIC),
        "cardinality": {c: int(frame[c].nunique()) for c in CATEGORICAL},
        "missing": {c: int(frame[c].isna().sum()) for c in frame.columns if frame[c].isna().any()},
    }
