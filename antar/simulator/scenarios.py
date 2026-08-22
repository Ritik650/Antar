"""The three parameterisations, plus the constant-effect validation mode.

docs/SIMULATOR_CARD.md section 8 defines these along four axes. Every headline
result is reported across all three, because a single number from a single
parameterisation is a number about our parameter choices and nothing else.

The critical discipline, from SIMULATOR_CARD section 12.4: **no parameter here is
named, added, or tuned to produce a particular finding.** Each scenario moves the
same four dials in the same direction. In particular there is no `sleeping_dogs_rate`
and no `is_sleeping_dog` flag anywhere in this package - the negative-uplift
population either emerges from the opt-out mechanism or it does not, and
`tests/statistical/test_anti_circularity.py` is what decides.

`conservative` is the scenario that can embarrass us. High self-heal means most
apparent recovery is spurious; high opt-out sensitivity means most outreach is
harmful. If Antar's advantage disappears there, we report it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from antar.simulator.rng import beta_from_mean


@dataclass(frozen=True)
class Scenario:
    """One complete parameterisation of the generative process."""

    name: str

    # --- the four axes of docs/SIMULATOR_CARD.md section 8 -------------------
    mean_self_heal: float
    """Probability a failed cycle succeeds on the next scheduled attempt with no
    intervention at all. **The single most consequential parameter in the system**
    and entirely invented - see SIMULATOR_CARD section 12.1."""

    mean_optout_sensitivity: float
    """Propensity to use the RBI-EM-02 per-transaction opt-out when prompted.
    Invented. The second most consequential parameter."""

    downtime_multiplier: float
    """Scales the injected downtime frequency from config."""

    heterogeneity: float
    """Scales dispersion in every latent. 1.0 is the reference. Higher means the
    treatment effect varies more across customers, which is what makes an uplift
    model worth having rather than a single average."""

    # --- secondary, moved in step with the axes above ------------------------
    intent_to_churn_rate: float
    """Latent desire to cancel, independent of payment failure."""

    base_failure_rate: float
    """Probability a cycle fails on first attempt, before segment health and
    balance effects."""

    mean_persuadability: float
    """Ceiling on P(persuaded | best channel, best timing). Invented."""

    # --- validation mode -----------------------------------------------------
    constant_effect: float | None = None
    """When set, all heterogeneity is switched off and every customer gets exactly
    this additive treatment effect. SIMULATOR_CARD section 5.3: a naive
    difference-in-means must recover it within its CI, or nothing downstream is
    trustworthy and the build stops."""

    notes: str = ""

    # --- derived -------------------------------------------------------------

    @property
    def self_heal_beta(self) -> tuple[float, float]:
        # Lower concentration -> wider spread. Heterogeneity widens the spread
        # without moving the mean, so the axes stay independent.
        return beta_from_mean(self.mean_self_heal, 12.0 / self.heterogeneity)

    @property
    def optout_beta(self) -> tuple[float, float]:
        return beta_from_mean(self.mean_optout_sensitivity, 10.0 / self.heterogeneity)

    @property
    def persuadability_beta(self) -> tuple[float, float]:
        return beta_from_mean(self.mean_persuadability, 9.0 / self.heterogeneity)

    @property
    def is_validation_mode(self) -> bool:
        return self.constant_effect is not None

    def as_dict(self) -> dict[str, Any]:
        """Flat view for the parameter table in docs/SIMULATOR_CARD.md section 8.

        That table's values are generated from here by `scripts/make_figures.py` and
        never typed by hand.
        """
        return {
            "name": self.name,
            "mean_self_heal": self.mean_self_heal,
            "mean_optout_sensitivity": self.mean_optout_sensitivity,
            "downtime_multiplier": self.downtime_multiplier,
            "heterogeneity": self.heterogeneity,
            "intent_to_churn_rate": self.intent_to_churn_rate,
            "base_failure_rate": self.base_failure_rate,
            "mean_persuadability": self.mean_persuadability,
            "self_heal_beta_alpha": round(self.self_heal_beta[0], 4),
            "self_heal_beta_beta": round(self.self_heal_beta[1], 4),
            "optout_beta_alpha": round(self.optout_beta[0], 4),
            "optout_beta_beta": round(self.optout_beta[1], 4),
            "constant_effect": self.constant_effect,
        }


CONSERVATIVE = Scenario(
    name="conservative",
    mean_self_heal=0.45,
    mean_optout_sensitivity=0.30,
    downtime_multiplier=1.6,
    heterogeneity=1.4,
    intent_to_churn_rate=0.22,
    base_failure_rate=0.16,
    mean_persuadability=0.22,
    notes=(
        "Hardest regime. Most apparent recovery is self-healing and most outreach "
        "provokes an opt-out. Included precisely because it can embarrass us."
    ),
)

BASE = Scenario(
    name="base",
    mean_self_heal=0.30,
    mean_optout_sensitivity=0.18,
    downtime_multiplier=1.0,
    heterogeneity=1.0,
    intent_to_churn_rate=0.15,
    base_failure_rate=0.14,
    mean_persuadability=0.28,
    notes="Reference parameterisation. No claim that it resembles any real merchant.",
)

AGGRESSIVE = Scenario(
    name="aggressive",
    mean_self_heal=0.18,
    mean_optout_sensitivity=0.10,
    downtime_multiplier=0.6,
    heterogeneity=0.7,
    intent_to_churn_rate=0.08,
    base_failure_rate=0.12,
    mean_persuadability=0.34,
    notes="Easiest regime. Little self-healing, tolerant customers, few outages.",
)

CONSTANT_EFFECT = Scenario(
    name="constant_effect",
    mean_self_heal=0.30,
    mean_optout_sensitivity=0.0001,  # effectively off; kept > 0 so Beta stays valid
    downtime_multiplier=0.0,
    heterogeneity=1.0,
    intent_to_churn_rate=0.0,
    base_failure_rate=0.20,
    mean_persuadability=0.20,
    constant_effect=0.12,
    notes=(
        "Validation mode only. Every treated cycle gets exactly +0.12 recovery "
        "probability. Never used for any reported result."
    ),
)

SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (CONSERVATIVE, BASE, AGGRESSIVE, CONSTANT_EFFECT)
}

# The three that appear in every reported table. `constant_effect` is deliberately
# excluded: it is a test instrument, not a regime.
REPORTED: tuple[str, ...] = ("conservative", "base", "aggressive")


def get_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}"
        ) from None


def scenario_table() -> list[dict[str, Any]]:
    """The rows behind the SIMULATOR_CARD section 8 table."""
    return [SCENARIOS[name].as_dict() for name in REPORTED]


# A guard against the failure mode SIMULATOR_CARD 12.4 warns about. If someone adds
# a scenario-specific dial later, this list is what they have to edit, and editing it
# is a visible act in a diff rather than a quiet one.
DECLARED_AXES: frozenset[str] = frozenset(
    {
        "mean_self_heal",
        "mean_optout_sensitivity",
        "downtime_multiplier",
        "heterogeneity",
        "intent_to_churn_rate",
        "base_failure_rate",
        "mean_persuadability",
    }
)

_STRUCTURAL_FIELDS: frozenset[str] = frozenset({"name", "constant_effect", "notes"})


def undeclared_parameters() -> set[str]:
    """Any Scenario field that is not a declared axis. Asserted empty by the tests."""
    return set(Scenario.__dataclass_fields__) - DECLARED_AXES - _STRUCTURAL_FIELDS
