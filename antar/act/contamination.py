"""TRAI-03: promotional content contaminates the whole message.

> if promotional content is mixed with any other type of communication (say, service
> or transactional) then such communication will be treated as promotional

One upsell sentence appended to a dunning message reclassifies the entire message as
promotional, which changes the required header, the number series, and whether DND
applies. It converts a lawful transactional send into an unlawful one.

**This is the rule an LLM is most likely to break, and it will break it while being
helpful.** "We also have 20% off annual plans" is a genuinely useful sentence to a
customer who is about to lapse. It is also a regulatory violation, and the model has no
way to know that.

## Hybrid, because either half alone is defeatable

  * **Deterministic rules** catch the obvious: discount language, offer patterns,
    urgency-plus-price constructions, referral asks. Fast, auditable, and impossible to
    argue with in an incident review.
  * **A learned classifier** catches phrasings nobody wrote a rule for.

Both paths are tested. PLAN.md section 9.4 names the positive and negative cases and
each one appears in `tests/adversarial/test_contamination.py`.

## The scoring convention

`score` is in [0, 1] and **higher means more promotional**. The gate blocks at
`act.contamination.classifier_threshold` (0.50 by default). A message that trips any
deterministic rule scores 1.0 regardless of what the classifier thinks, because a rule
that a model score can override is not a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from antar.signals.schemas import MessageClass


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    explanation: str


def _rule(rule_id: str, pattern: str, explanation: str) -> Rule:
    return Rule(rule_id, re.compile(pattern, re.IGNORECASE), explanation)


# Numbers written as words. The first version of DISCOUNT_OFFER matched only digits,
# and the adversarial suite immediately produced "we also have twenty percent off
# annual plans" - schema-valid, type-valid, and straight past the rule. A model writing
# prose writes numbers as words far more often than as digits, so a digits-only
# discount rule was checking the case least likely to occur. See POSTMORTEM D19.
NUMBER_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
    r"twenty|twenty[- ]five|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
)
QUANTITY = rf"(?:\d{{1,3}}|{NUMBER_WORD})"


# Written from the question "what makes this promotional?", not from a list of words we
# happened to think of. Each rule names a *construction* rather than a keyword, because
# a keyword list is a game of whack-a-mole and a construction is not.
RULES: tuple[Rule, ...] = (
    _rule(
        "DISCOUNT_OFFER",
        rf"\b{QUANTITY}\s?(%|per\s?cent|percent)\s*(off|discount|cashback|back)\b",
        "An explicit percentage discount, in digits or in words. The clearest "
        "possible promotional signal.",
    ),
    _rule(
        "MONEY_OFF",
        rf"\b(flat|save|get)\s*(rs\.?|inr)?\s?{QUANTITY}\s*(off|cashback|back)\b",
        "A rupee-denominated offer. Distinct from stating the amount owed, "
        "which every recovery message must do.",
    ),
    _rule(
        "OFF_A_PLAN",
        r"\boff\s+(our|your|the|all|annual|monthly|yearly|premium)\b",
        "A discount applied to a plan. Catches the construction when the quantity is "
        "phrased in a way no quantity pattern anticipated - which is the failure mode "
        "a quantity pattern always eventually has.",
    ),
    _rule(
        "UPGRADE_PITCH",
        r"\b(upgrade|switch|move)\s+(to|your)\s+\w*\s*(annual|premium|pro|plus|yearly)\b",
        "An upsell to a different plan. Not a recovery message.",
    ),
    _rule(
        "OFFER_NOUN",
        r"\b(limited[- ]time|special|exclusive|introductory)\s+(offer|deal|price|rate)\b",
        "Offer framing. Language that exists to make a price feel like a "
        "concession has no place in a payment-failure notice.",
    ),
    _rule(
        "REFERRAL",
        r"\b(refer|invite)\s+(a\s+)?(friend|friends|someone)\b",
        "A referral ask is promotional even inside a service message.",
    ),
    _rule(
        "CROSS_SELL",
        r"\b(also|additionally|by the way|btw)\b[^.]{0,60}\b(try|check out|explore|discover)\b",
        "A cross-sell appended to an otherwise transactional message - the exact shape "
        "TRAI-03 describes.",
    ),
    _rule(
        "FREE_TRIAL",
        r"\b(free)\s+(trial|month|delivery|gift)\b",
        "A free-something inducement. An offer by another name, and one that "
        "reclassifies the whole message under TRAI-03.",
    ),
    _rule(
        "URGENCY_MARKETING",
        r"\b(hurry|last chance|ends (today|soon|tonight)|do ?n.?t miss)\b",
        "Marketing urgency. A payment deadline is stated as a date, not as urgency.",
    ),
    _rule(
        "NEW_FEATURE",
        r"\b(new|introducing|now available)\b[^.]{0,40}\b(feature|plan|tier|product)\b",
        "Product announcement. A customer whose payment just failed is not "
        "the audience for a launch.",
    ),
)


@dataclass
class ContaminationVerdict:
    score: float
    blocked: bool
    triggered_rules: list[str] = field(default_factory=list)
    classifier_score: float = 0.0
    explanation: str = ""
    reclassified_as: MessageClass | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "blocked": self.blocked,
            "triggered_rules": self.triggered_rules,
            "classifier_score": round(self.classifier_score, 4),
            "explanation": self.explanation,
            "reclassified_as": (
                self.reclassified_as.value if self.reclassified_as else None
            ),
        }


class ContaminationDetector:
    """Rules first, classifier second, rules win.

    A deterministic rule that a model score can override is not a rule, so a rule
    match pins the score at 1.0. The classifier only ever *raises* the score.
    """

    def __init__(self, *, threshold: float = 0.50, classifier: Any = None) -> None:
        self.threshold = threshold
        self.classifier = classifier

    def inspect(
        self, text: str, *, declared_class: MessageClass = MessageClass.TRANSACTIONAL
    ) -> ContaminationVerdict:
        triggered = [rule for rule in RULES if rule.pattern.search(text)]
        classifier_score = self._classifier_score(text)

        if triggered:
            names = ", ".join(r.id for r in triggered)
            return ContaminationVerdict(
                score=1.0,
                blocked=True,
                triggered_rules=[r.id for r in triggered],
                classifier_score=classifier_score,
                reclassified_as=MessageClass.PROMOTIONAL,
                explanation=(
                    f"TRAI-03: promotional content detected ({names}). "
                    + " ".join(r.explanation for r in triggered)
                    + f" A {declared_class.value} message containing this is treated as "
                    "PROMOTIONAL in its entirety, which changes the required header and "
                    "number series and brings DND into scope. Blocked."
                ),
            )

        blocked = classifier_score >= self.threshold
        return ContaminationVerdict(
            score=classifier_score,
            blocked=blocked,
            triggered_rules=[],
            classifier_score=classifier_score,
            reclassified_as=MessageClass.PROMOTIONAL if blocked else None,
            explanation=(
                f"No deterministic rule matched; the classifier scored "
                f"{classifier_score:.2f} against a threshold of {self.threshold:.2f}."
                + (" Blocked." if blocked else " Passed.")
            ),
        )

    def _classifier_score(self, text: str) -> float:
        if self.classifier is None:
            return 0.0
        try:
            return float(self.classifier.score(text))
        except Exception:
            # Fail closed on the classifier only, not on the whole message: the
            # deterministic rules have already run and are the load-bearing half.
            return 0.0

    @classmethod
    def from_config(cls, config, *, classifier: Any = None) -> ContaminationDetector:
        return cls(
            threshold=float(config.get("act.contamination.classifier_threshold")),
            classifier=classifier,
        )


class LexicalClassifier:
    """A small, transparent stand-in for a learned model.

    Scores on promotional-register vocabulary that the deterministic rules do not
    cover - the words that make a sentence *feel* like marketing without matching any
    single construction.

    Deliberately simple and deliberately explainable. A black-box classifier deciding
    whether a message is lawful would be a strange choice in a system whose entire
    argument is that the language model does not make decisions.
    """

    MARKETING_TERMS: tuple[str, ...] = (
        "deal", "offer", "save", "bonus", "reward", "unlock", "exclusive",
        "premium", "upgrade", "discount", "promo", "voucher", "coupon",
        "cashback", "sale", "bargain", "freebie", "perks",
    )

    TRANSACTIONAL_TERMS: tuple[str, ...] = (
        "payment", "failed", "retry", "mandate", "debit", "due", "amount",
        "cancel", "opt-out", "optout", "authorise", "authorize", "update",
    )

    def score(self, text: str) -> float:
        words = re.findall(r"[a-z\-]+", text.lower())
        if not words:
            return 0.0
        marketing = sum(1 for w in words if w in self.MARKETING_TERMS)
        transactional = sum(1 for w in words if w in self.TRANSACTIONAL_TERMS)
        if marketing == 0:
            return 0.0
        # A message with heavy transactional vocabulary gets the benefit of the doubt
        # for one stray marketing word; two or more outweigh it.
        raw = marketing / (marketing + 0.5 * transactional + 1.0)
        return float(min(max(raw * 1.6, 0.0), 1.0))


def default_detector(config=None) -> ContaminationDetector:
    from antar.config import get_config

    return ContaminationDetector.from_config(
        config or get_config(), classifier=LexicalClassifier()
    )


__all__ = [
    "RULES",
    "ContaminationDetector",
    "ContaminationVerdict",
    "LexicalClassifier",
    "Rule",
    "default_detector",
]
