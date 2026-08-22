"""Hidden customer state. **This is the answer key.**

Nothing in `antar/signals`, `antar/detect`, `antar/decide`, `antar/policy`, or
`antar/act` may read a `CustomerLatents`. If any of these values reaches
`antar/decide/features.py` - even indirectly, through a derived column - the uplift
models will look spectacular and mean nothing. That is the classic uplift-project
failure, and `tests/statistical/test_no_leakage.py` is the cheap insurance against
it.

Every distribution here is an **author choice**. docs/SIMULATOR_CARD.md section 4.1
records which are anchored to something external (very few) and which are invented
(nearly all). Keep the two in sync; a test asserts that they are.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from antar.signals.schemas import Channel
from antar.simulator.rng import substream
from antar.simulator.scenarios import Scenario

# Weakly anchored: Indian salary credits cluster at month start and month end.
# The *shape* is anchored; the exact weights are ours.
SALARY_DAYS: tuple[int, ...] = (1, 5, 7, 10, 25, 30)
SALARY_DAY_WEIGHTS: tuple[float, ...] = (0.30, 0.14, 0.10, 0.10, 0.16, 0.20)

RESPONSE_CHANNELS: tuple[Channel, ...] = (
    Channel.SMS,
    Channel.WHATSAPP,
    Channel.VOICE,
    Channel.EMAIL,
)

# Dirichlet concentration. Low values give customers a sharply preferred channel,
# which is most of what makes channel choice worth optimising.
CHANNEL_ALPHA: tuple[float, ...] = (1.4, 1.8, 0.7, 0.9)


@dataclass(frozen=True)
class CustomerLatents:
    """Everything true about a customer that Antar is not allowed to know."""

    customer_id: str

    p_self_heal_base: float
    """P(next scheduled cycle succeeds | no intervention). Invented."""

    salary_day: int
    balance_half_life_days: float
    """How fast the account drains after payday. Invented, mechanistically motivated."""

    channel_response: dict[Channel, float]
    """Relative responsiveness by channel, summing to 1. Invented."""

    persuadability: float
    """Ceiling on P(persuaded) under a perfectly chosen, perfectly timed contact."""

    optout_sensitivity: float
    """Propensity to use the RBI-EM-02 opt-out when a notification arrives."""

    price_sensitivity: float
    """Response to discount size. Invented."""

    tenure_months: int
    intent_to_churn: bool
    """Latent desire to cancel, independent of payment failure. Drives the
    sleeping-dogs population emergently - there is no sleeping-dog flag."""

    # ------------------------------------------------------------------ model

    def balance_fraction(self, when: datetime) -> float:
        """Funds availability on a 0-1 scale at a given instant.

        Exponential decay from the salary day, wrapping at the month boundary. This
        is what makes *timing* a lever: the same message on day 3 and day 22 hits a
        very different account.
        """
        day = when.day
        days_since_salary = (day - self.salary_day) % 30
        decay = math.exp(-math.log(2.0) * days_since_salary / self.balance_half_life_days)
        # A floor, because nobody is at exactly zero and a hard zero would make the
        # timing term degenerate.
        return float(0.08 + 0.92 * decay)

    def best_channel(self) -> Channel:
        return max(self.channel_response, key=lambda c: self.channel_response[c])

    def next_balance_peak(self, after: datetime, *, hour: int = 11) -> datetime:
        """The next instant at or after `after` when this customer has most money.

        Provided as a method rather than left to each caller because getting it wrong
        is easy and silent: an earlier version of the anti-circularity scan computed
        the peak as `replace(day=min(salary_day, 28))`, which for a customer paid on
        the 30th lands 29 days *after* payday - the trough, not the peak - and so
        overstated the negative-uplift population by evaluating treatment at its
        worst possible timing while claiming to evaluate it at its best.

        Searched rather than derived, because month lengths, the 30-day wrap in
        `balance_fraction`, and the TRAI contact window interact in ways that are
        tedious to reason about and cheap to brute-force.
        """
        candidates = [
            (after + timedelta(days=offset)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            for offset in range(0, 32)
        ]
        candidates = [c for c in candidates if c >= after]
        return max(candidates, key=self.balance_fraction)

    def next_balance_trough(self, after: datetime, *, hour: int = 11) -> datetime:
        """The mirror of `next_balance_peak`. Used only by tests."""
        candidates = [
            (after + timedelta(days=offset)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            for offset in range(0, 32)
        ]
        candidates = [c for c in candidates if c >= after]
        return min(candidates, key=self.balance_fraction)

    def as_row(self) -> dict[str, float | int | str | bool]:
        """Flat view for the ground-truth frame. Used by tests and the simulator
        card's calibration report; never joined into a feature matrix."""
        return {
            "customer_id": self.customer_id,
            "p_self_heal_base": self.p_self_heal_base,
            "salary_day": self.salary_day,
            "balance_half_life_days": self.balance_half_life_days,
            "persuadability": self.persuadability,
            "optout_sensitivity": self.optout_sensitivity,
            "price_sensitivity": self.price_sensitivity,
            "tenure_months": self.tenure_months,
            "intent_to_churn": self.intent_to_churn,
            **{f"channel_response_{c.value}": v for c, v in self.channel_response.items()},
        }


def draw_latents(customer_id: str, scenario: Scenario, seed: int) -> CustomerLatents:
    """Draw one customer's hidden vector.

    The substream is keyed on the customer id, so customer 900's latents are the
    same whether we generate 10 customers or 10,000, and adding a draw elsewhere in
    the simulator cannot perturb them.
    """
    rng = substream(seed, "latents", customer_id)

    if scenario.is_validation_mode:
        return _constant_effect_latents(customer_id, scenario, rng)

    self_alpha, self_beta = scenario.self_heal_beta
    opt_alpha, opt_beta = scenario.optout_beta
    per_alpha, per_beta = scenario.persuadability_beta

    channel_weights = rng.dirichlet(np.array(CHANNEL_ALPHA) / scenario.heterogeneity)

    return CustomerLatents(
        customer_id=customer_id,
        p_self_heal_base=float(rng.beta(self_alpha, self_beta)),
        salary_day=int(rng.choice(SALARY_DAYS, p=SALARY_DAY_WEIGHTS)),
        balance_half_life_days=float(np.clip(rng.lognormal(mean=2.1, sigma=0.45), 2.0, 28.0)),
        channel_response={c: float(w) for c, w in zip(RESPONSE_CHANNELS, channel_weights, strict=True)},
        persuadability=float(rng.beta(per_alpha, per_beta)),
        optout_sensitivity=float(rng.beta(opt_alpha, opt_beta)),
        price_sensitivity=float(np.clip(rng.lognormal(mean=0.0, sigma=0.6), 0.15, 4.0)),
        tenure_months=int(np.clip(rng.geometric(p=0.06), 1, 96)),
        intent_to_churn=bool(rng.random() < scenario.intent_to_churn_rate),
    )


def _constant_effect_latents(
    customer_id: str, scenario: Scenario, rng: np.random.Generator
) -> CustomerLatents:
    """Validation mode: heterogeneity off, so the planted effect is recoverable.

    SIMULATOR_CARD section 5.3. Everything that could make the treatment effect vary
    across customers is pinned to a constant; only `p_self_heal_base` keeps a small
    spread, because a completely degenerate population would not exercise the
    estimator at all.
    """
    return CustomerLatents(
        customer_id=customer_id,
        p_self_heal_base=float(np.clip(rng.normal(scenario.mean_self_heal, 0.05), 0.02, 0.95)),
        salary_day=15,
        balance_half_life_days=14.0,
        channel_response=dict.fromkeys(RESPONSE_CHANNELS, 0.25),
        persuadability=scenario.mean_persuadability,
        optout_sensitivity=0.0,
        price_sensitivity=1.0,
        tenure_months=12,
        intent_to_churn=False,
    )


class LatentStore:
    """Lazily-materialised latents, keyed by customer.

    Deliberately not a plain dict handed around the codebase: everything that can
    read the answer key goes through this object, so `test_no_leakage` has exactly
    one thing to check for in the layers that must not touch it.
    """

    def __init__(self, scenario: Scenario, seed: int) -> None:
        self.scenario = scenario
        self.seed = seed
        self._cache: dict[str, CustomerLatents] = {}

    def get(self, customer_id: str) -> CustomerLatents:
        latents = self._cache.get(customer_id)
        if latents is None:
            latents = draw_latents(customer_id, self.scenario, self.seed)
            self._cache[customer_id] = latents
        return latents

    def __len__(self) -> int:
        return len(self._cache)

    def rows(self) -> list[dict[str, float | int | str | bool]]:
        return [self._cache[cid].as_row() for cid in sorted(self._cache)]
