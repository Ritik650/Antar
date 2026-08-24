"""L3 may not select an action L4 cannot perform.

M8's end-to-end pipeline chose `VOICE` for an event and the drafter raised
`no registered template for channel VOICE`. The decide layer's action set was
`CONTACT_CHANNELS`; the act layer's was whatever happened to be in `registry.yaml`, and
nothing compared the two. POSTMORTEM D20.

These tests derive both sets from the code rather than restating them, so adding a
channel to the enum fails here until a template exists for it - which is the
ADR-0013 pattern applied to a second seam.
"""

from __future__ import annotations

import pytest

from antar.act.drafter import DEFAULT_TEMPLATE, TEMPLATE_FOR, choose_template
from antar.act.templates import TemplateError, load_templates
from antar.signals.schemas import CONTACT_CHANNELS, Channel, FailureClass


def test_every_contact_channel_has_a_template():
    """The completeness check that did not exist when D20 happened."""
    missing = sorted(c.value for c in CONTACT_CHANNELS if c not in DEFAULT_TEMPLATE)
    assert not missing, (
        f"L3 may select {missing} but L4 has no default template for them. Either "
        "register a template or remove the channel from CONTACT_CHANNELS - a "
        "decision that cannot be executed is a bug, not a degraded mode."
    )


# A revoked mandate has no truthful short message on a push channel: it cannot be
# retried and cannot be re-debited. `choose_template` refuses rather than substituting
# a plausible sentence.
REFUSED = {
    (channel, FailureClass.MANDATE_REVOKED)
    for channel in CONTACT_CHANNELS
    if channel is not Channel.EMAIL
}


@pytest.mark.parametrize("channel", sorted(CONTACT_CHANNELS, key=lambda c: c.value))
@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_every_channel_and_failure_class_pair_resolves(channel, failure_class):
    """The cross product, not a sample of it.

    `TEMPLATE_FOR` overrides some pairs and `DEFAULT_TEMPLATE` catches the rest; only
    exhausting both axes shows that the fallback actually covers what the overrides
    do not.
    """
    if (channel, failure_class) in REFUSED:
        with pytest.raises(TemplateError, match="revoked mandate"):
            choose_template(channel, failure_class)
        return

    template = choose_template(channel, failure_class)
    assert template.channel is channel, (
        f"{channel.value}/{failure_class.value} resolved to {template.id}, which is "
        f"registered for {template.channel.value}"
    )


def test_silent_retry_has_no_template_and_should_not():
    """A silent retry sends nothing, so a message template would be a category error."""
    assert Channel.SILENT_RETRY not in DEFAULT_TEMPLATE
    assert Channel.SILENT_RETRY not in CONTACT_CHANNELS


def test_every_referenced_template_id_exists():
    """A mapping to a template id that `registry.yaml` does not define fails at send
    time, on the one path where failing late is most expensive."""
    registered = set(load_templates())
    referenced = set(DEFAULT_TEMPLATE.values()) | set(TEMPLATE_FOR.values())
    assert referenced <= registered, f"unregistered ids referenced: {referenced - registered}"


def test_every_registered_template_is_reachable():
    """The other direction. A template nothing can select is dead weight in a registry
    whose whole value is that it is small enough to read."""
    registered = set(load_templates())
    reachable = {
        choose_template(channel, failure_class).id
        for channel in CONTACT_CHANNELS
        for failure_class in FailureClass
        if (channel, failure_class) not in REFUSED
    }
    assert registered == reachable, (
        f"unreachable templates: {sorted(registered - reachable)}"
    )


# ------------------------------------------- resolving is not the same as being right


RETRY_PROMISE = ("we will retry", "we will try again")

# Classes where an unattended retry cannot succeed, so a message promising one is a
# false statement - not a suboptimal choice of words.
NO_RETRY_POSSIBLE = {
    FailureClass.AFA_REQUIRED,
    FailureClass.TECHNICAL_DECLINE,
    FailureClass.MANDATE_REVOKED,
}


@pytest.mark.parametrize("channel", sorted(CONTACT_CHANNELS, key=lambda c: c.value))
@pytest.mark.parametrize("failure_class", sorted(NO_RETRY_POSSIBLE, key=lambda f: f.value))
def test_a_retry_is_never_promised_where_a_retry_cannot_work(channel, failure_class):
    """The invariant `test_every_contact_channel_has_a_template` does not cover.

    Adding `RETRY_SCHEDULED_VOICE` made every VOICE pair *resolve*, and the coverage
    test went green while an AFA_REQUIRED event was being told "we will try again on
    2 Apr" - for a charge that cannot succeed unattended. A test that checks a lookup
    returns something says nothing about whether the something is true. POSTMORTEM D20.
    """
    if (channel, failure_class) in REFUSED:
        pytest.skip("no template exists for this pair, by design")

    body = choose_template(channel, failure_class).body.lower()
    offending = [phrase for phrase in RETRY_PROMISE if phrase in body]
    assert not offending, (
        f"{channel.value}/{failure_class.value} resolves to a template promising "
        f"{offending}, which cannot happen for this failure class."
    )


def test_a_retry_promise_is_fine_where_a_retry_can_work():
    """The other direction, so the test above cannot pass by there being no retry
    language anywhere - which would make it vacuous."""
    body = choose_template(Channel.SMS, FailureClass.INSUFFICIENT_FUNDS).body.lower()
    assert any(phrase in body for phrase in RETRY_PROMISE), (
        "no template promises a retry at all, so the invariant above proves nothing"
    )
