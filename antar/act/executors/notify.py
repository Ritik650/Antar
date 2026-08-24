"""Send the message.

Moves no money, and is gated anyway. Three reasons, none of them about rupees:

  * **A message is a regulated act.** Sending outside the TRAI-01 window, on the wrong
    number series, or to someone who has opted out is a violation whether or not a
    payment follows.
  * **A message consumes a scarce resource.** Every send spends a `C-BUDGET` contact
    slot, and under RBI-EM-02 it delivers a cancel prompt. The opt-out hazard is the
    single largest cost term in the L3 objective, which makes an ungated send more
    expensive in expectation than an ungated small charge.
  * **The ledger has to be complete.** A trace that answers "why did you contact this
    customer at 11:04 on a Tuesday?" cannot have sends in it that never went through
    the gate.

## No real provider

There is no SMS gateway behind this. `DeliveryTransport` is a protocol with an
in-memory implementation, and `docs/LIMITATIONS.md` L11 says plainly that no template
here is DLT-registered and no number series is allocated. What is exercised is the
*decision* to send and everything guarding it, not the delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from antar.policy.gate import requires_gate
from antar.signals.schemas import ActionRecord, Channel, MessageClass

# TRAI-05: promotional voice uses the 140 series, transactional and service use
# 1600/160. Antar never sends promotional, so it should only ever use the second.
NUMBER_SERIES: dict[MessageClass, str] = {
    MessageClass.TRANSACTIONAL: "1600",
    MessageClass.SERVICE: "1600",
    MessageClass.PROMOTIONAL: "140",
}


class DeliveryTransport(Protocol):
    """Whatever actually delivers. Narrow, so a test can substitute a list."""

    def deliver(self, *, channel: Channel, header: str, series: str, body: str) -> str: ...


@dataclass
class InMemoryTransport:
    """Records sends instead of making them. The only transport that exists."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def deliver(self, *, channel: Channel, header: str, series: str, body: str) -> str:
        reference = f"msg_{len(self.sent):08d}"
        self.sent.append(
            {
                "reference": reference,
                "channel": channel.value,
                "header": header,
                "series": series,
                "body": body,
            }
        )
        return reference


class DeliveryRefused(Exception):
    """A send that would be unlawful. Raised rather than returned, because a caller
    ignoring a return value would send it anyway."""


@requires_gate
def send_notification(
    action: ActionRecord,
    *,
    transport: DeliveryTransport,
    header: str = "ANTARP",
    disclose_autodialer: bool = False,
) -> str:
    """Deliver the drafted message. Returns the provider reference.

    The last line of defence, and deliberately redundant with the gate: if a
    `PROMOTIONAL` record ever reached here, that would mean the gate had been bypassed,
    and the right response to that is to refuse rather than to trust that it cannot
    happen.
    """
    if action.draft is None:
        raise DeliveryRefused(
            f"{action.action_id} has no drafted message. An executor cannot compose "
            "one: bodies come from registered templates via the drafter."
        )

    if action.message_class is MessageClass.PROMOTIONAL:
        raise DeliveryRefused(
            "TRAI-03: refusing to send a PROMOTIONAL recovery message. Reaching here "
            "means the gate was bypassed, which is a bug worth failing loudly for."
        )

    if action.draft.message_class is not action.message_class:
        # A draft whose class disagrees with the action it is attached to means the
        # contamination detector reclassified it and something downstream did not
        # notice. The number series would then be wrong on the wire.
        raise DeliveryRefused(
            f"class mismatch: action says {action.message_class.value}, draft says "
            f"{action.draft.message_class.value}. The draft was probably reclassified "
            "by the contamination detector."
        )

    series = NUMBER_SERIES[action.message_class]
    body = action.draft.rendered

    if action.channel is Channel.VOICE and disclose_autodialer:
        # TRAI-07 as actually written: the auto-dialer declaration is a registration
        # duty owed to the Originating Access Provider, not a line read to the
        # customer. PLAN.md had this the other way round; see the regulatory register.
        # The flag is recorded on the action for auditability and changes no content.
        pass

    return transport.deliver(
        channel=action.channel, header=header, series=series, body=body
    )


def series_for(message_class: MessageClass) -> str:
    return NUMBER_SERIES[message_class]
