"""The second opinion: a deliberately stupid constraint checker.

PLAN.md M6 asks for "a second, naive validator; the LP and the validator must agree -
this is a real bug-catcher". It is written here, in M4, alongside the compiler rather
than after the allocator, because the bugs it catches are *compiler* bugs and there is
no reason to let them live for two milestones.

## Why a second implementation rather than more tests

The interesting failures in `compiler.py` are off-by-one errors on a threshold: a
23-hour lead time that slips through because a comparison used `>` where the
regulation says "at least", or a contact budget that permits `k+1` because the count
of already-spent contacts was applied on the wrong side. Unit tests do not catch those
reliably, because the person writing the test has the same wrong number in their head
as the person who wrote the code.

Two implementations that disagree are a different kind of evidence. So this module
shares **no code** with the compiler. It does not import `compile_problem`, it does not
reuse the predicates, and it re-derives every threshold from the regulation objects
directly. It is O(n^2) in places and does not care.

## The rule

Any solution the allocator returns must pass `validate()`. A disagreement is a bug in
one of the two, and the correct response is to find out which - never to make the
validator agree.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise
from typing import Any

from antar.policy.compiler import Candidate
from antar.signals.schemas import CONTACT_CHANNELS, ConsentBasis, MandateState, MessageClass


@dataclass(frozen=True)
class Violation:
    rule: str
    candidate_id: str
    detail: str


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def __bool__(self) -> bool:
        return self.ok

    def report(self) -> str:
        if self.ok:
            return "valid: no constraint violated"
        lines = [f"{len(self.violations)} violation(s):"]
        lines += [f"  [{v.rule}] {v.candidate_id}: {v.detail}" for v in self.violations]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [
                {"rule": v.rule, "candidate_id": v.candidate_id, "detail": v.detail}
                for v in self.violations
            ],
        }


def validate(
    selected: Sequence[Candidate],
    *,
    contacts_per_30d: int = 3,
    cooldown_hours: int = 72,
    lead_time_hours: int = 24,
    window_start_hour: int = 10,
    window_end_hour: int = 21,
    explicit_consent_validity_days: int = 7,
    margin_budget_paise: int | None = None,
) -> ValidationResult:
    """Check a chosen set of actions against every constraint, the slow obvious way.

    Thresholds are passed in explicitly rather than read from config, so that a
    caller cannot accidentally validate against the same misconfigured value the
    compiler used.
    """
    result = ValidationResult()

    # ---- per-candidate rules ------------------------------------------------
    for candidate in selected:
        ctx = candidate.context
        action = ctx.intervention
        customer = ctx.customer
        cid = candidate.candidate_id
        is_contact = action.channel in CONTACT_CHANNELS

        # RBI-EM-01: "at least 24 hours prior". At least means >=.
        hours_ahead = (action.scheduled_for - ctx.now).total_seconds() / 3600.0
        if hours_ahead < lead_time_hours:
            result.violations.append(
                Violation(
                    "RBI-EM-01",
                    cid,
                    f"scheduled {hours_ahead:.2f}h ahead, needs at least {lead_time_hours}h",
                )
            )

        # RBI-EM-02
        if customer.optout_received:
            result.violations.append(
                Violation("RBI-EM-02", cid, "customer has exercised an opt-out")
            )

        # RBI-EM-03 / -04
        if ctx.event.amount_paise > ctx.afa_free_ceiling_paise and not action.requires_afa:
            result.violations.append(
                Violation(
                    "RBI-EM-03/04",
                    cid,
                    f"Rs {ctx.event.amount_paise / 100:,.0f} exceeds the "
                    f"Rs {ctx.afa_free_ceiling_paise / 100:,.0f} ceiling for "
                    f"{ctx.event.merchant_category.value} but is not routed via AFA",
                )
            )

        # RBI-EM-06
        if customer.mandate_state not in (MandateState.ACTIVE, MandateState.HALTED):
            result.violations.append(
                Violation("RBI-EM-06", cid, f"mandate is {customer.mandate_state.value}")
            )

        if is_contact:
            # TRAI-01. Recomputed from the hour directly rather than by calling the
            # clock helper the compiler uses.
            hour = action.scheduled_for.hour
            if not (window_start_hour <= hour < window_end_hour):
                result.violations.append(
                    Violation(
                        "TRAI-01",
                        cid,
                        f"scheduled at {hour:02d}:xx, outside "
                        f"{window_start_hour:02d}:00-{window_end_hour:02d}:00",
                    )
                )

            # TRAI-03
            if action.message_class is MessageClass.PROMOTIONAL:
                result.violations.append(
                    Violation("TRAI-03", cid, "promotional class on a recovery contact")
                )

            # TRAI-04
            if customer.consent_basis is ConsentBasis.NONE:
                result.violations.append(Violation("TRAI-04", cid, "no consent basis"))
            elif customer.consent_basis is ConsentBasis.EXPLICIT:
                if customer.consent_granted_at is None:
                    result.violations.append(
                        Violation("TRAI-04", cid, "explicit consent with no timestamp")
                    )
                elif ctx.now - customer.consent_granted_at > timedelta(
                    days=explicit_consent_validity_days
                ):
                    result.violations.append(
                        Violation("TRAI-04", cid, "explicit consent has expired")
                    )
            elif customer.mandate_state in (MandateState.REVOKED, MandateState.COMPLETED):
                result.violations.append(
                    Violation("TRAI-04", cid, "inferred consent ended with the contract")
                )

            # POL-PROMISE
            if customer.promise_to_pay_at is not None:
                result.violations.append(
                    Violation("POL-PROMISE", cid, "a promise to pay is on record")
                )

            # POL-COOLDOWN against history
            if customer.last_contact_at is not None:
                gap = (action.scheduled_for - customer.last_contact_at).total_seconds() / 3600.0
                if gap < cooldown_hours:
                    result.violations.append(
                        Violation(
                            "POL-COOLDOWN",
                            cid,
                            f"{gap:.1f}h since the last contact, needs {cooldown_hours}h",
                        )
                    )

    # ---- cross-candidate rules ----------------------------------------------
    per_event: dict[str, int] = defaultdict(int)
    per_customer: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in selected:
        per_event[candidate.event_id] += 1
        if candidate.is_contact:
            per_customer[candidate.customer_id].append(candidate)

    for event_id, count in sorted(per_event.items()):
        if count > 1:
            result.violations.append(
                Violation("ONE_ACTION_PER_EVENT", event_id, f"{count} actions on one cycle")
            )

    for customer_id, group in sorted(per_customer.items()):
        already = group[0].context.customer.contacts_in_window
        if already + len(group) > contacts_per_30d:
            result.violations.append(
                Violation(
                    "POL-BUDGET",
                    customer_id,
                    f"{already} already spent + {len(group)} selected exceeds "
                    f"{contacts_per_30d} per 30 days",
                )
            )

        # Pairwise cooldown, the obvious O(n^2) way.
        times = sorted(c.context.intervention.scheduled_for for c in group)
        for earlier, later in pairwise(times):
            gap = (later - earlier).total_seconds() / 3600.0
            if gap < cooldown_hours:
                result.violations.append(
                    Violation(
                        "POL-COOLDOWN",
                        customer_id,
                        f"two selected contacts {gap:.1f}h apart, needs {cooldown_hours}h",
                    )
                )

    if margin_budget_paise is not None:
        spent = sum(c.discount_paise for c in selected)
        if spent > margin_budget_paise:
            result.violations.append(
                Violation(
                    "POL-MARGIN",
                    "batch",
                    f"Rs {spent / 100:,.0f} of discount exceeds the "
                    f"Rs {margin_budget_paise / 100:,.0f} margin budget",
                )
            )

    return result


def cross_check(problem, selection: dict[str, float], **kwargs) -> ValidationResult:
    """Validate a solver's answer against the naive checker.

    `selection` maps candidate id to 0/1. Also asserts the solver did not select a
    candidate the compiler had already ruled infeasible, which is the failure mode
    where a bug in the compiler and a bug in the allocator cancel out and nobody
    notices.
    """
    by_id = {c.candidate_id: c for c in problem.feasible}
    chosen: list[Candidate] = []
    result = ValidationResult()

    for candidate_id, value in selection.items():
        if value <= 0.5:
            continue
        candidate = by_id.get(candidate_id)
        if candidate is None:
            result.violations.append(
                Violation(
                    "INFEASIBLE_SELECTED",
                    candidate_id,
                    "the solver selected a candidate the compiler had rejected",
                )
            )
            continue
        chosen.append(candidate)

    inner = validate(chosen, **kwargs)
    result.violations.extend(inner.violations)
    return result
