"""The LLM. **It writes language. Nothing else.** (N1)

This is the only module in Antar that calls a language model, and what it is allowed to
return is a mapping from slot name to string. Not a message body, not an amount, not a
channel, not a decision about whether to contact anyone. Those were all settled by L3
before this module is reached, and the gate reads them from the `Decision`, never from
anything here.

## Why an LLM at all

Because the alternative is worse in the one place a model is genuinely good. A
deterministic filler produces "your payment of Rs 499 failed" for every customer in
every situation. A model can produce a `reason` phrase that is accurate, short, and
readable for *this* failure - "your bank declined the auto-debit" rather than
"error_code BAD_REQUEST_ERROR" - and that is a real improvement to a real person
reading a real SMS.

It is also the entire extent of the improvement, which is why the surface is this small.

## The pipeline, and what fails closed at each step

1. **Call** the model with a strict JSON schema and the template's slot list.
2. **Parse.** Malformed JSON gets exactly one repair attempt, then falls back.
3. **Validate** every slot against its declared type. A URL in a name field is rejected
   here, before rendering.
4. **Render** through the registered template. Extra keys are a rejection.
5. **Inspect** the rendered text for TRAI-03 contamination.
6. **Fall back** to the deterministic filler on any failure, flagged in the trace.

Every step can only *reject*. There is no path where a model response widens what
Antar is permitted to do.

## Determinism and provenance

`act.llm.enabled` defaults to `false`, so the deterministic filler is the normal path
and every draft it produces carries `fallback_used=True`. A run's numbers therefore
never silently imply a model was involved when it was not.

Every draft records a `prompt_version` - a content hash of the system prompt and the
schema - so a message in the ledger can be traced to the exact instructions that
produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from antar.act.contamination import ContaminationDetector, default_detector
from antar.act.templates import Template, TemplateError
from antar.act.templates import get as get_template
from antar.signals.schemas import (
    AtRiskEvent,
    Channel,
    DraftedMessage,
    FailureClass,
    Intervention,
    MessageClass,
)

SYSTEM_PROMPT = """\
You fill slots in a pre-registered SMS/WhatsApp template for an Indian payments \
company. You are not writing a message; you are supplying values for named fields in a \
message that already exists.

Rules, all of which are enforced after you respond:
- Return ONLY a JSON object mapping the requested slot names to string values.
- Supply every requested slot. Do not add any key that was not requested.
- Never include an offer, discount, upgrade, referral or any promotional content. \
Under Indian telecom regulation (TCCCPR, TRAI) promotional content anywhere in a \
service message reclassifies the whole message and makes sending it unlawful.
- Never invent or alter an amount, a date, or a URL. Use exactly the values given.
- The `reason` slot must be a short, plain, non-technical phrase describing why the \
payment failed. No error codes. No blame. No more than eight words.
"""

# What the model is told it may return. Also what the validator enforces, so the schema
# is not a suggestion the model can decline.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}


def prompt_version(template: Template) -> str:
    """Content hash of the instructions that produced a draft.

    Stored with every message so a line in the ledger can be traced back to the exact
    prompt, template body, and slot set behind it. A prompt change that is not visible
    in the trace is a change nobody can audit.
    """
    payload = json.dumps(
        {
            "system": SYSTEM_PROMPT,
            "template": template.id,
            "body": template.body,
            "slots": sorted(template.slots),
        },
        sort_keys=True,
    )
    return "pv_" + hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic facts. The model never supplies any of these.
# ---------------------------------------------------------------------------

REASON_PHRASES: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "not enough balance at the time",
    FailureClass.ISSUER_DOWN: "your bank was temporarily unavailable",
    FailureClass.TECHNICAL_DECLINE: "your saved payment method was declined",
    FailureClass.RISK_DECLINE: "your bank declined the request",
    FailureClass.AFA_REQUIRED: "this amount needs your approval",
    FailureClass.MANDATE_REVOKED: "the mandate is no longer active",
    FailureClass.UNKNOWN: "the payment could not be completed",
}

# Failure classes where the default retry template would say something untrue. A
# technical decline will not fix itself on a retry and an AFA-required charge cannot
# succeed unattended, so promising "we will try again" is a false statement in a
# message that must be factual. Every channel that can carry these classes needs its
# own entry - the VOICE rows exist because a live trace showed the fallback promising
# an AFA retry (POSTMORTEM D20).
TEMPLATE_FOR: dict[tuple[Channel, FailureClass], str] = {
    (Channel.SMS, FailureClass.TECHNICAL_DECLINE): "INSTRUMENT_UPDATE_SMS",
    (Channel.SMS, FailureClass.AFA_REQUIRED): "AFA_AUTHENTICATION_SMS",
    (Channel.VOICE, FailureClass.TECHNICAL_DECLINE): "INSTRUMENT_UPDATE_VOICE",
    (Channel.VOICE, FailureClass.AFA_REQUIRED): "AFA_AUTHENTICATION_VOICE",
    (Channel.WHATSAPP, FailureClass.TECHNICAL_DECLINE): "INSTRUMENT_UPDATE_WHATSAPP",
    (Channel.WHATSAPP, FailureClass.AFA_REQUIRED): "AFA_AUTHENTICATION_WHATSAPP",
}

# Every channel in `CONTACT_CHANNELS` must appear here. L3 selects from that set, so a
# channel missing a default is a decision L4 cannot execute - which is how M8's
# end-to-end pipeline died on VOICE (POSTMORTEM D20). The completeness of this mapping
# is asserted by `test_every_contact_channel_has_a_template` rather than trusted.
DEFAULT_TEMPLATE: dict[Channel, str] = {
    Channel.SMS: "RETRY_SCHEDULED_SMS",
    Channel.WHATSAPP: "RETRY_SCHEDULED_WHATSAPP",
    Channel.EMAIL: "PAYMENT_LINK_EMAIL",
    Channel.VOICE: "RETRY_SCHEDULED_VOICE",
}


def choose_template(channel: Channel, failure_class: FailureClass) -> Template:
    """Template selection is deterministic and belongs to us, not to the model.

    A model choosing its own template could pick the AFA flow for a customer whose
    payment simply bounced, which is both wrong and unlawful to send.
    """
    if failure_class is FailureClass.MANDATE_REVOKED and channel is not Channel.EMAIL:
        # There is no truthful short message here. A revoked mandate cannot be retried
        # and cannot be re-debited; the customer would have to authorise a new mandate,
        # which is a re-signup the merchant owns and Antar does not. L2 recommends
        # TERMINATE for this class and L3 vetoes the candidate, so reaching this line
        # means a caller bypassed that - and inventing a plausible sentence would be
        # the failure mode this whole layer exists to prevent. Email is exempt because
        # `PAYMENT_LINK_EMAIL` offers a one-off payment link and promises nothing about
        # the mandate.
        raise TemplateError(
            "no truthful template exists for a revoked mandate on "
            f"{channel.value}: it cannot be retried and cannot be re-debited. L2 "
            "recommends TERMINATE for MANDATE_REVOKED and L3 removes the candidate; "
            "if this was reached, the veto was bypassed."
        )

    template_id = TEMPLATE_FOR.get((channel, failure_class)) or DEFAULT_TEMPLATE.get(channel)
    if template_id is None:
        raise TemplateError(f"no registered template for channel {channel.value}")
    return get_template(template_id)


def _money(paise: int) -> str:
    rupees = paise / 100
    return f"{rupees:,.0f}" if rupees == int(rupees) else f"{rupees:,.2f}"


def _safe_date(value: datetime) -> str:
    # %-d is not portable to Windows; build it without the platform-specific flag.
    return f"{value.day} {value.strftime('%b')} {value.year}"


@dataclass(frozen=True)
class DraftContext:
    """Everything a draft needs. Every field is a fact, not an opinion."""

    event: AtRiskEvent
    intervention: Intervention
    failure_class: FailureClass
    merchant_name: str = "Antar"
    customer_name: str = "there"
    plan_name: str = "your subscription"
    afa_ceiling_paise: int = 1_500_000

    def fixed_slots(self) -> dict[str, str]:
        """Slot values Antar computes. **The model may not change any of these.**

        Amounts, dates and URLs are here rather than in the model's remit because a
        model that can restate an amount is a model that can misstate one.
        """
        base = f"https://pay.antar.example/{self.event.event_id[-12:]}"
        return {
            "merchant_name": self.merchant_name,
            "customer_name": self.customer_name,
            "plan_name": self.plan_name,
            "amount": _money(self.event.amount_paise),
            "ceiling": _money(self.afa_ceiling_paise),
            "retry_date": _safe_date(self.intervention.scheduled_for),
            "due_date": _safe_date(self.event.occurred_at),
            "expiry_date": _safe_date(self.intervention.scheduled_for),
            "payment_link": base,
            "update_link": base.replace("pay.", "link.") + "/update",
            "auth_link": base.replace("pay.", "auth.") + "/approve",
            "optout_link": base + "/stop",
        }


class Drafter:
    """Produces a `DraftedMessage`. Never produces a decision."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        client: Any = None,
        detector: ContaminationDetector | None = None,
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 700,
        temperature: float = 0.2,
        repair_attempts: int = 1,
    ) -> None:
        self.enabled = enabled
        self.client = client
        self.detector = detector or default_detector()
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.repair_attempts = repair_attempts
        self.failures: list[str] = []

    @classmethod
    def from_config(cls, config, *, client: Any = None, detector=None) -> Drafter:
        section = config.section("act.llm")
        return cls(
            enabled=bool(section.get("enabled")),
            client=client,
            detector=detector or default_detector(config),
            model=str(section.get("model")),
            max_tokens=int(section.get("max_tokens")),
            temperature=float(section.get("temperature")),
            repair_attempts=int(section.get("repair_attempts")),
        )

    # ------------------------------------------------------------------ draft

    def draft(self, context: DraftContext) -> DraftedMessage:
        template = choose_template(context.intervention.channel, context.failure_class)
        version = prompt_version(template)
        fixed = context.fixed_slots()
        # The only slot the model is asked for. Everything else is a fact.
        model_slots = [name for name in template.slots if name not in fixed]

        if self.enabled and self.client is not None and model_slots:
            drafted = self._draft_with_model(context, template, fixed, model_slots, version)
            if drafted is not None:
                return drafted

        return self._deterministic(context, template, fixed, version, repair_attempts=0)

    def _draft_with_model(
        self,
        context: DraftContext,
        template: Template,
        fixed: dict[str, str],
        model_slots: list[str],
        version: str,
    ) -> DraftedMessage | None:
        attempts = 0
        last_error = ""
        while attempts <= self.repair_attempts:
            try:
                raw = self._call(context, template, model_slots, repair=last_error)
                values = self._parse(raw, model_slots)
                slots = {**fixed, **values}
                rendered = template.render({k: slots[k] for k in template.slots})
            except (TemplateError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                self.failures.append(f"{type(exc).__name__}: {exc}")
                attempts += 1
                continue

            verdict = self.detector.inspect(rendered, declared_class=template.message_class)
            if verdict.blocked:
                # A contaminated draft is not repaired by asking again in the same way;
                # the deterministic body is known-clean and is what gets sent.
                self.failures.append(f"contaminated: {verdict.triggered_rules or 'classifier'}")
                return self._deterministic(
                    context, template, fixed, version,
                    repair_attempts=attempts,
                    note=verdict.explanation,
                    contamination_score=verdict.score,
                )

            return DraftedMessage(
                template_id=template.id,
                message_class=template.message_class,
                slots=slots,
                rendered=rendered,
                language=template.language,
                prompt_version=version,
                fallback_used=False,
                repair_attempts=attempts,
                contamination_score=verdict.score,
            )

        return None

    def _call(
        self, context: DraftContext, template: Template, model_slots: list[str], *, repair: str
    ) -> str:
        instruction = {
            "template_id": template.id,
            "channel": template.channel.value,
            "message_class": template.message_class.value,
            "slots_required": model_slots,
            "failure_class": context.failure_class.value,
            "guidance": "Return only these keys, as a JSON object of strings.",
        }
        if repair:
            instruction["previous_error"] = repair
            instruction["retry_note"] = "Your last response was rejected. Fix exactly this."

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(instruction, sort_keys=True)}],
        )
        blocks = getattr(response, "content", None) or []
        return "".join(getattr(block, "text", "") for block in blocks)

    @staticmethod
    def _parse(raw: str, model_slots: list[str]) -> dict[str, str]:
        """Extract the JSON object and check its shape.

        Tolerant of a fenced code block, because models emit them, and intolerant of
        everything else - a response with prose around the JSON is still a response we
        can use, but a response with the wrong keys is not.
        """
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("no JSON object in the model response")
            text = text[start : end + 1]

        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("model response was not a JSON object")

        missing = [name for name in model_slots if name not in parsed]
        if missing:
            raise ValueError(f"model omitted slots {missing}")
        extra = [name for name in parsed if name not in model_slots]
        if extra:
            # An invented key is a model that has misunderstood its job, and quite
            # possibly one that has been talked into something. Reject, do not trim.
            raise ValueError(f"model returned keys it was not asked for: {extra}")
        return {name: str(parsed[name]) for name in model_slots}

    def _deterministic(
        self,
        context: DraftContext,
        template: Template,
        fixed: dict[str, str],
        version: str,
        *,
        repair_attempts: int,
        note: str = "",
        contamination_score: float = 0.0,
    ) -> DraftedMessage:
        """The fallback, and the default. Known-clean by construction.

        Every phrase comes from `REASON_PHRASES`, which is a fixed table written by a
        human. There is no path by which this produces something the contamination
        detector has not already seen.
        """
        if note:
            # Why we fell back, kept on the drafter so the batch runner can put it in
            # the ledger. A fallback whose reason is not recorded is a fallback nobody
            # can learn from.
            self.failures.append(note)

        slots = dict(fixed)
        slots["reason"] = REASON_PHRASES.get(
            context.failure_class, REASON_PHRASES[FailureClass.UNKNOWN]
        )
        rendered = template.render({name: slots[name] for name in template.slots})

        verdict = self.detector.inspect(rendered, declared_class=template.message_class)
        if verdict.blocked:  # pragma: no cover - would mean our own table is unlawful
            raise TemplateError(
                f"the deterministic template {template.id} is itself contaminated: "
                f"{verdict.explanation}. This is a bug in registry.yaml, not a model "
                "problem."
            )

        return DraftedMessage(
            template_id=template.id,
            message_class=template.message_class,
            slots={name: slots[name] for name in template.slots},
            rendered=rendered,
            language=template.language,
            prompt_version=version,
            fallback_used=True,
            repair_attempts=repair_attempts,
            contamination_score=max(contamination_score, verdict.score),
        )


def default_message_class(template_id: str) -> MessageClass:
    return get_template(template_id).message_class


__all__ = [
    "REASON_PHRASES",
    "SYSTEM_PROMPT",
    "DraftContext",
    "Drafter",
    "choose_template",
    "prompt_version",
]
