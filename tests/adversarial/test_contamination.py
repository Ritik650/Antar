"""TRAI-03 contamination: the cases PLAN.md section 9.4 names, one test each.

> **Positive cases (must block):** dunning message with an upsell appended; renewal
> reminder with a discount code for a *different* product; service message containing a
> referral ask.
>
> **Negative cases (must pass):** plain retry notification; pre-debit notification with
> amount and opt-out link; failure explanation with a payment link for the *same* dues.

The negative cases matter as much as the positive ones. A detector that blocks
everything is trivially safe and completely useless — it would stop Antar sending the
notification RBI-EM-01 *requires*.
"""

from __future__ import annotations

import pytest

from antar.act.contamination import (
    RULES,
    ContaminationDetector,
    LexicalClassifier,
    default_detector,
)
from antar.config import load_config
from antar.signals.schemas import MessageClass

pytestmark = pytest.mark.adversarial


@pytest.fixture
def detector() -> ContaminationDetector:
    return default_detector(load_config(environ={}))


# ----------------------------------------------------- positive: must block


POSITIVE_CASES: list[tuple[str, str]] = [
    (
        "upsell appended to dunning",
        "Antar: your payment of Rs 499 could not be completed. We will retry on "
        "26 Aug 2026. Also, upgrade to annual and save Rs 500!",
    ),
    (
        "upsell in words rather than digits",
        "Your payment failed. We also have twenty percent off annual plans.",
    ),
    (
        "discount code for a different product",
        "Reminder: your subscription renews on 1 Sep. Use code MUSIC50 for 50% off "
        "our music plan.",
    ),
    (
        "referral ask inside a service message",
        "We could not charge your card. Please update it. Refer a friend and get a "
        "free month.",
    ),
    (
        "cross-sell framing",
        "Your payment did not go through. Also, check out our new premium tier.",
    ),
    (
        "marketing urgency",
        "Payment failed. Hurry, this exclusive offer ends tonight.",
    ),
    (
        "free trial inducement",
        "Your mandate could not be debited. Start a free trial of our annual plan.",
    ),
]


@pytest.mark.parametrize(("label", "text"), POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES])
def test_promotional_content_is_blocked(detector, label, text):
    verdict = detector.inspect(text, declared_class=MessageClass.TRANSACTIONAL)
    assert verdict.blocked, f"{label}: not blocked (score {verdict.score:.2f})"
    assert verdict.reclassified_as is MessageClass.PROMOTIONAL
    assert verdict.explanation


def test_a_blocked_message_explains_which_rule_fired(detector):
    verdict = detector.inspect("Payment failed. Get 20% off annual plans.")
    assert verdict.triggered_rules
    assert "TRAI-03" in verdict.explanation
    # The explanation has to say what happens as a consequence, not just that it
    # matched - an operator reading a refusal needs to know why it matters.
    assert "header" in verdict.explanation or "number series" in verdict.explanation


# ------------------------------------------------------ negative: must pass


NEGATIVE_CASES: list[tuple[str, str]] = [
    (
        "plain retry notification",
        "Antar: your payment of Rs 499 for Monthly Plan could not be completed (not "
        "enough balance at the time). We will retry on 26 Aug 2026.",
    ),
    (
        "pre-debit notification with amount and opt-out",
        "Antar: we will debit Rs 499 for Monthly Plan on 26 Aug 2026. To skip this "
        "payment or cancel the mandate, reply STOP or visit "
        "https://pay.antar.example/abc/stop.",
    ),
    (
        "failure explanation with a link for the same dues",
        "Antar: your payment of Rs 499 did not go through (your bank declined the "
        "request). You can complete it here: https://pay.antar.example/abc.",
    ),
    (
        "instrument update request",
        "Antar: we could not charge your saved payment method for Monthly Plan. "
        "Please update it at https://link.antar.example/abc/update.",
    ),
    (
        "AFA authentication request",
        "Antar: your payment of Rs 24,500 needs your approval because it is above the "
        "Rs 15,000 limit for automatic debits. Approve at "
        "https://auth.antar.example/abc/approve.",
    ),
]


@pytest.mark.parametrize(("label", "text"), NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES])
def test_lawful_recovery_messages_pass(detector, label, text):
    """A detector that blocks everything is trivially safe and completely useless.

    It would stop Antar sending the pre-debit notification RBI-EM-01 *requires*, which
    would be a compliance failure caused by a compliance control.
    """
    verdict = detector.inspect(text, declared_class=MessageClass.TRANSACTIONAL)
    assert not verdict.blocked, (
        f"{label}: lawful message blocked (score {verdict.score:.2f}, "
        f"rules {verdict.triggered_rules})"
    )


def test_every_registered_template_passes_its_own_detector():
    """Our own message bodies must be lawful to send.

    If a registered template trips the detector, that is a bug in `registry.yaml` and
    the drafter raises rather than falling back - there would be nothing clean to fall
    back to.
    """
    from antar.act.templates import load_templates

    detector = default_detector(load_config(environ={}))
    for template in load_templates().values():
        # Render with placeholder-free text so the body itself is what is judged.
        skeleton = template.body
        for slot in template.slots:
            skeleton = skeleton.replace("{" + slot + "}", "VALUE")
        verdict = detector.inspect(skeleton, declared_class=template.message_class)
        assert not verdict.blocked, (
            f"registered template {template.id} is itself contaminated: "
            f"{verdict.triggered_rules}"
        )


# ------------------------------------------------------------- both paths


def test_the_deterministic_path_alone_catches_the_obvious(detector):
    """PLAN.md section 9.4 asks for both paths to be tested. This is the rules half."""
    rules_only = ContaminationDetector(threshold=0.5, classifier=None)
    verdict = rules_only.inspect("Payment failed. Get 20% off annual plans.")
    assert verdict.blocked
    # Two constructions fire here - the percentage and the "off <plan>" phrasing - and
    # overlapping coverage is the intent rather than a redundancy to trim. A message
    # that evades one rule should still meet another.
    assert "DISCOUNT_OFFER" in verdict.triggered_rules
    assert verdict.classifier_score == 0.0


def test_the_classifier_alone_catches_what_no_rule_names():
    """And this is the classifier half.

    "exclusive premium deal reward" matches no single construction - `OFFER_NOUN`
    needs `exclusive` adjacent to `offer`/`deal`, and `premium` sits between them - but
    the promotional register is unmistakable.
    """
    classifier_only = ContaminationDetector(threshold=0.5, classifier=LexicalClassifier())
    verdict = classifier_only.inspect(
        "Payment failed. We have an exclusive premium deal reward for you."
    )
    assert verdict.blocked
    assert verdict.triggered_rules == []
    assert verdict.classifier_score >= 0.5


def test_a_rule_match_outranks_a_confident_classifier():
    """A rule a model score can override is not a rule."""

    class AlwaysClean:
        def score(self, text: str) -> float:
            return 0.0

    detector = ContaminationDetector(threshold=0.5, classifier=AlwaysClean())
    verdict = detector.inspect("Payment failed. Get 20% off annual plans.")
    assert verdict.blocked
    assert verdict.score == 1.0


def test_a_broken_classifier_does_not_open_the_gate():
    """Failing closed on the classifier, not on the message.

    The deterministic rules have already run and are the load-bearing half, so a
    classifier that throws degrades the detector rather than disabling it.
    """

    class Exploding:
        def score(self, text: str) -> float:
            raise RuntimeError("model unavailable")

    detector = ContaminationDetector(threshold=0.5, classifier=Exploding())
    assert detector.inspect("Payment failed. Get 20% off annual plans.").blocked
    assert not detector.inspect("Antar: your payment of Rs 499 failed.").blocked


def test_every_rule_has_an_explanation_and_a_distinct_id():
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    for rule in RULES:
        assert len(rule.explanation) > 20, rule.id


def test_the_threshold_comes_from_config():
    config = load_config(environ={})
    detector = default_detector(config)
    assert detector.threshold == float(config.get("act.contamination.classifier_threshold"))


# ------------------------------------------------ the claim LIMITATIONS L15 makes


def test_the_two_halves_carry_the_load_limitations_l15_says_they_do():
    """L15 publishes a table. This is the code that produces it.

    A documented number that nothing recomputes decays silently. If someone widens the
    lexicon or adds a rule, this test fails and `docs/LIMITATIONS.md` gets updated in
    the same commit - which is the only way the table stays true.
    """
    classifier = LexicalClassifier()
    rules_only = ContaminationDetector(threshold=0.5, classifier=None)

    rules_block = sum(1 for _, text in POSITIVE_CASES if rules_only.inspect(text).blocked)
    classifier_blocks = sum(1 for _, text in POSITIVE_CASES if classifier.score(text) >= 0.5)
    false_positives = sum(1 for _, text in NEGATIVE_CASES if classifier.score(text) >= 0.5)

    assert len(POSITIVE_CASES) == 7
    assert rules_block == 7, "L15 says the deterministic rules block 7 of 7"
    assert classifier_blocks == 3, (
        f"L15 says the classifier alone clears threshold on 3 of 7; measured "
        f"{classifier_blocks}. Update docs/LIMITATIONS.md in this commit."
    )
    assert false_positives == 0, "L15 says the classifier costs nothing in false positives"
