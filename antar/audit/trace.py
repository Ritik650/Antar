"""Reassemble one event's whole story from the ledger.

PLAN.md M8:

> `trace.py`: given an `event_id`, assemble the full decision trace — features, uplift
> estimate and CI, chosen action, binding constraints, model and policy versions, cost,
> outcome.
>
> Console answers *"why did you contact this customer at 11:04 on a Tuesday?"* in one
> click with a complete trace.

That question has a specific shape. It is not "what did you do" — it is **"why that,
and why then"**, and the two halves have different answers. *That* comes from the
diagnosis and the uplift estimate. *Then* comes from the constraints: TRAI-01's window,
the customer's balance-peak hour, the C-BUDGET spacing. A trace that answers only the
first half sounds like a rationalisation.

## The trace is derived, never stored

Nothing writes a `Trace`. It is assembled by reading the ledger, which means it cannot
drift from the record and cannot be written to say something the record does not.
`Trace.verified` carries the chain result for the entries it was built from, so a trace
shown in the console is accompanied by whether the ledger it came from is intact.

## Missing stages are reported, not filled in

An event diagnosed but never decided, or decided but never acted on, is a completely
normal state — most events end at "no action was worth taking". `Trace.gaps` names
which stages are absent and `narrate()` says so in words. Inferring a plausible missing
stage would be the exact failure this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from antar.audit.ledger import Ledger, VerificationResult, verify_entries
from antar.signals.schemas import LedgerEntry, LedgerKind

STAGES: tuple[LedgerKind, ...] = (
    LedgerKind.EVENT,
    LedgerKind.DIAGNOSIS,
    LedgerKind.DECISION,
    LedgerKind.ACTION,
    LedgerKind.OUTCOME,
)


@dataclass(frozen=True)
class Trace:
    event_id: str
    entries: tuple[LedgerEntry, ...]
    verified: VerificationResult
    event: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    actions: tuple[dict[str, Any], ...] = ()
    outcome: dict[str, Any] | None = None
    alerts: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------ shape

    @property
    def found(self) -> bool:
        return bool(self.entries)

    @property
    def gaps(self) -> list[str]:
        """Stages with no entry. Named rather than filled in."""
        present = {entry.kind for entry in self.entries}
        return [stage.value for stage in STAGES if stage not in present]

    @property
    def executed(self) -> bool:
        """A send actually reached a provider.

        Distinct from `approved` on purpose. Under `dry_run` the gate approves an
        action and deliberately does not execute it, so a trace can legitimately show
        a full approved action with `executed=False`. Collapsing the two would make
        every dry-run trace read as a refusal.
        """
        return any(a.get("executed") for a in self.actions)

    @property
    def approved(self) -> bool:
        """The gate let it through, whether or not it was then sent."""
        return any(not a.get("rejected_reason") for a in self.actions)

    @property
    def dry_run(self) -> bool:
        return bool(self.actions) and all(a.get("dry_run") for a in self.actions)

    @property
    def refusals(self) -> list[dict[str, Any]]:
        return [a for a in self.actions if a.get("rejected_reason")]

    # ------------------------------------------------- the answer fields

    @property
    def contacted_at(self) -> datetime | None:
        for action in self.actions:
            stamp = action.get("attempted_at")
            if stamp:
                return datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        return None

    @property
    def binding_constraints(self) -> list[str]:
        return list((self.decision or {}).get("binding_constraints", []))

    @property
    def uplift(self) -> tuple[float, tuple[float, float]] | None:
        if not self.decision:
            return None
        ci = self.decision.get("uplift_ci", [0.0, 0.0])
        return float(self.decision.get("uplift_estimate", 0.0)), (float(ci[0]), float(ci[1]))

    @property
    def versions(self) -> dict[str, str]:
        """Every version that touched this event. Reproducibility starts here."""
        out: dict[str, str] = {}
        if self.diagnosis:
            out["detector"] = str(self.diagnosis.get("model_version", "unversioned"))
        if self.decision:
            out["uplift_model"] = str(self.decision.get("model_version", "unversioned"))
            out["policy"] = str(self.decision.get("policy_version", "unversioned"))
        prompts = sorted(
            {
                str((a.get("draft") or {}).get("prompt_version"))
                for a in self.actions
                if (a.get("draft") or {}).get("prompt_version")
            }
        )
        if prompts:
            # Plural on purpose. Two drafts on one event can come from two prompt
            # versions if a deploy landed between them, and collapsing that to one
            # would make the trace claim a reproducibility it does not have.
            out["prompt"] = ", ".join(prompts)
        return out

    @property
    def money(self) -> dict[str, int]:
        outcome = self.outcome or {}
        return {
            "amount_paise": int((self.event or {}).get("amount_paise", 0)),
            "expected_incremental_paise": int(
                (self.decision or {}).get("expected_incremental_paise", 0)
            ),
            "cost_paise": int(
                outcome.get("cost_paise", sum(int(a.get("cost_paise", 0)) for a in self.actions))
            ),
            "discount_paise": int(
                outcome.get(
                    "discount_paise", sum(int(a.get("discount_paise", 0)) for a in self.actions)
                )
            ),
            "recovered_paise": int(outcome.get("recovered_paise", 0)),
        }

    # -------------------------------------------------------- rendering

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "found": self.found,
            "ledger_verified": self.verified.as_dict(),
            "gaps": self.gaps,
            "event": self.event,
            "diagnosis": self.diagnosis,
            "decision": self.decision,
            "actions": list(self.actions),
            "outcome": self.outcome,
            "alerts": list(self.alerts),
            "versions": self.versions,
            "money": self.money,
            "narrative": self.narrate(),
        }

    def narrate(self) -> str:
        """Plain prose. The one-click answer.

        Assembled entirely from ledger fields; every sentence names the field it came
        from. Nothing here is generated by a language model - N1 permits the LLM to
        write customer-facing language, and an audit trace is not that.
        """
        if not self.found:
            return f"No ledger entries exist for {self.event_id}."

        lines: list[str] = []

        if self.event:
            amount = self.money["amount_paise"] / 100
            lines.append(
                f"A payment of Rs {amount:,.2f} for {self.event.get('customer_id', 'a customer')} "
                f"failed with code {self.event.get('error_code', 'unknown')}."
            )

        if self.diagnosis:
            confidence = float(self.diagnosis.get("confidence", 0.0))
            lines.append(
                f"L2 classified it as {self.diagnosis.get('failure_class')} at "
                f"{confidence:.0%} confidence via {self.diagnosis.get('source')}, and "
                f"recommended {self.diagnosis.get('recommended_class')}."
            )
            if self.diagnosis.get("downtime_overlap"):
                lines.append(
                    "The failure overlapped a declared downtime window, which is why "
                    "the recommendation is to wait rather than to contact."
                )

        if self.decision:
            uplift, (low, high) = self.uplift or (0.0, (0.0, 0.0))
            if self.decision.get("chosen"):
                chosen = self.decision["chosen"]
                lines.append(
                    f"L3 estimated an uplift of {uplift:+.4f} (95% CI {low:+.4f} to "
                    f"{high:+.4f}) and chose {chosen.get('channel')} scheduled for "
                    f"{chosen.get('scheduled_for')}."
                )
            else:
                lines.append(
                    f"L3 estimated an uplift of {uplift:+.4f} (95% CI {low:+.4f} to "
                    f"{high:+.4f}) and chose to take no action."
                )
            if self.decision.get("is_control"):
                lines.append(
                    "This event was in the randomised holdout, so no action was taken "
                    "regardless of the estimate (N3)."
                )
            if self.decision.get("stopping_rule_fired"):
                lines.append(
                    f"A stopping rule fired: {self.decision['stopping_rule_fired']}."
                )
            binding = [c for c in self.binding_constraints if c != "RANDOMISED_HOLDOUT"]
            if binding:
                lines.append(
                    "The binding constraints were " + ", ".join(binding) + "."
                )
            elif not self.decision.get("is_control"):
                lines.append(
                    "No constraint was binding on this event; the schedule reflects the "
                    "estimated best moment rather than a limit."
                )
            rationale = str(self.decision.get("rationale") or "")
            # The control sentence above already says this; repeating the rationale
            # verbatim reads like a system that has lost track of what it just said.
            if rationale and not self.decision.get("is_control"):
                lines.append(rationale)

        for action in self.actions:
            if action.get("rejected_reason"):
                lines.append(
                    f"The gate refused action {action.get('action_id')}: "
                    f"{action['rejected_reason']} Nothing was sent."
                )
            elif action.get("requires_human_approval") and not action.get("approved_by"):
                lines.append(
                    f"Action {action.get('action_id')} is above the human-approval "
                    "threshold and is waiting for a person."
                )
            elif action.get("executed"):
                lines.append(
                    f"{action.get('channel')} sent at {action.get('attempted_at')} "
                    f"(reference {action.get('provider_reference') or 'none'})."
                )
                draft_text = (action.get("draft") or {}).get("rendered")
                if draft_text:
                    lines.append(f'The message was: "{draft_text}"')
                draft = action.get("draft") or {}
                if draft.get("fallback_used"):
                    lines.append(
                        "The language model's draft was not used; the deterministic "
                        "template fallback produced the text that went out."
                    )
            elif action.get("dry_run"):
                lines.append(
                    f"The gate approved {action.get('channel')} at "
                    f"{action.get('attempted_at')} and the run is in dry-run, so it "
                    "was deliberately not sent."
                )
                draft_text = (action.get("draft") or {}).get("rendered")
                if draft_text:
                    lines.append(f'The message would have been: "{draft_text}"')

        if self.outcome:
            money = self.money
            if self.outcome.get("recovered"):
                lines.append(
                    f"Recovered Rs {money['recovered_paise'] / 100:,.2f} against a cost "
                    f"of Rs {(money['cost_paise'] + money['discount_paise']) / 100:,.2f}."
                )
            else:
                lines.append(
                    f"Not recovered. Cost incurred: Rs "
                    f"{(money['cost_paise'] + money['discount_paise']) / 100:,.2f}."
                )
            if self.outcome.get("optout"):
                lines.append("The customer opted out. This is counted against the policy.")
            if self.outcome.get("mandate_revoked"):
                lines.append("The mandate was revoked.")

        if self.gaps:
            lines.append(
                "No ledger entry exists for: " + ", ".join(self.gaps) + ". "
                "That is reported rather than inferred."
            )

        if not self.verified.ok:
            lines.append(
                f"WARNING: the ledger does not verify - "
                f"{len(self.verified.breaks)} break(s), first at seq "
                f"{self.verified.first_break.seq}. Treat this trace as unreliable."
            )

        return " ".join(lines)


def _first(entries: list[LedgerEntry], kind: LedgerKind) -> dict[str, Any] | None:
    for entry in entries:
        if entry.kind is kind:
            return entry.payload
    return None


def _mentions(payload: dict[str, Any], key: str, value: str) -> bool:
    return payload.get(key) == value


def _assemble(
    event_id: str, entries: list[LedgerEntry], verified: VerificationResult
) -> Trace:
    decision = _first(entries, LedgerKind.DECISION)
    return Trace(
        event_id=event_id,
        entries=tuple(entries),
        verified=verified,
        event=_first(entries, LedgerKind.EVENT),
        diagnosis=_first(entries, LedgerKind.DIAGNOSIS),
        decision=decision,
        actions=tuple(e.payload for e in entries if e.kind is LedgerKind.ACTION),
        outcome=_first(entries, LedgerKind.OUTCOME),
        alerts=tuple(e.payload for e in entries if e.kind is LedgerKind.ALERT),
    )


def build_trace(ledger: Ledger, event_id: str) -> Trace:
    """Everything the ledger knows about one event, in order.

    Entries are found by `event_id`, then by the decision id and action id they carry,
    because a partial write could carry one and not the other.

    **Single-trace path.** This reads and verifies the whole ledger, which is right for
    one trace and quadratic for all of them. `TraceIndex` exists for the second case and
    is what `replay.py` and the console use - an earlier version of this module had only
    this function, and a full-size batch (3,436 events, ~14,000 entries) took long enough
    to look like a hang. See POSTMORTEM D26.
    """
    all_entries = ledger.entries()
    verified = verify_entries(all_entries)
    return _assemble(event_id, _slice_for(all_entries, event_id), verified)


def _slice_for(all_entries: list[LedgerEntry], event_id: str) -> list[LedgerEntry]:
    entries = [e for e in all_entries if _mentions(e.payload, "event_id", event_id)]

    decision = _first(entries, LedgerKind.DECISION)
    if decision and decision.get("decision_id"):
        seen = {e.seq for e in entries}
        entries += [
            e
            for e in all_entries
            if e.seq not in seen and _mentions(e.payload, "decision_id", decision["decision_id"])
        ]

    action = _first(entries, LedgerKind.ACTION)
    if action and action.get("action_id"):
        seen = {e.seq for e in entries}
        entries += [
            e
            for e in all_entries
            if e.seq not in seen and _mentions(e.payload, "action_id", action["action_id"])
        ]

    return sorted(entries, key=lambda e: e.seq)


@dataclass
class TraceIndex:
    """Every trace in a ledger, built from **one** pass over it.

    `build_trace` reads and verifies the entire ledger per call. Called once, that is
    correct. Called once per event on a full batch it is quadratic in the number of
    entries, and the batch runner does exactly that: 3,436 events against ~14,000
    entries is 48 million row constructions, plus 3,436 full chain verifications.

    So this loads once, groups by event id, and verifies once. The traces it returns are
    identical to `build_trace`'s - `test_the_index_and_the_single_trace_agree` asserts
    that on every event of a real batch rather than trusting it.
    """

    ledger: Ledger
    _entries: list[LedgerEntry] = field(default_factory=list)
    _by_event: dict[str, list[LedgerEntry]] = field(default_factory=dict)
    _verified: VerificationResult | None = None

    def _load(self) -> None:
        if self._entries:
            return
        self._entries = self.ledger.entries()
        self._verified = verify_entries(self._entries)

        by_decision: dict[str, str] = {}
        by_action: dict[str, str] = {}
        grouped: dict[str, list[LedgerEntry]] = {}

        # One pass in sequence order. An entry that names an event id joins that event;
        # one that names only a decision or action id joins whichever event claimed that
        # id earlier, which is exactly what the per-event scan resolved the slow way.
        for entry in self._entries:
            payload = entry.payload
            event_id = payload.get("event_id")
            if not isinstance(event_id, str):
                decision_id = payload.get("decision_id")
                action_id = payload.get("action_id")
                event_id = by_decision.get(str(decision_id)) or by_action.get(str(action_id))
            if not isinstance(event_id, str):
                continue

            grouped.setdefault(event_id, []).append(entry)
            if isinstance(payload.get("decision_id"), str):
                by_decision.setdefault(payload["decision_id"], event_id)
            if isinstance(payload.get("action_id"), str):
                by_action.setdefault(payload["action_id"], event_id)

        self._by_event = grouped

    @property
    def verified(self) -> VerificationResult:
        self._load()
        assert self._verified is not None
        return self._verified

    def event_ids(self) -> list[str]:
        self._load()
        return list(self._by_event)

    def trace(self, event_id: str) -> Trace:
        self._load()
        return _assemble(event_id, self._by_event.get(event_id, []), self.verified)

    def traces(self) -> list[Trace]:
        return [self.trace(event_id) for event_id in self.event_ids()]


__all__ = ["STAGES", "Trace", "TraceIndex", "build_trace"]
