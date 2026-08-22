"""The event-sourced cycle generator.

Produces a batch of at-risk recurring-payment cycles for a simulated Indian
merchant, with the ground truth kept strictly to one side. What comes out is
indistinguishable, to every layer downstream, from a stream of Razorpay webhooks.

Determinism is the contract (PLAN.md 9.3): a fixed seed gives a byte-identical
stream. Every draw comes from a keyed substream, so adding a draw in one place
cannot shift the numbers anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from antar import clock
from antar.config import Config, get_config
from antar.ids import deterministic_id
from antar.ids import event_id as make_event_id
from antar.signals.downtime import DowntimeRegistry
from antar.signals.schemas import (
    HIGH_CEILING_CATEGORIES,
    AtRiskEvent,
    CustomerContext,
    DowntimeSeverity,
    DowntimeStatus,
    DowntimeWindow,
    FailureClass,
    LossClass,
    MandateState,
    MerchantCategory,
    PaymentMethod,
    SegmentHealth,
)
from antar.simulator import failure_emission
from antar.simulator.latents import LatentStore
from antar.simulator.rng import substream
from antar.simulator.scenarios import Scenario, get_scenario

ISSUERS: tuple[str, ...] = ("HDFC", "ICICI", "SBI", "AXIS")

# Shares of *non-downtime, non-AFA* failures by true cause. Invented; see
# docs/SIMULATOR_CARD.md section 4.2 and 12.5.
AFA_SHARE_ABOVE_CEILING = 0.65
"""Share of above-ceiling failures whose true cause is the missing additional factor.

Invented, like every share here. Deliberately below 1.0 so that the AFA class is not
inferable from the amount alone.
"""

BASE_CAUSE_SHARES: dict[FailureClass, float] = {
    FailureClass.INSUFFICIENT_FUNDS: 0.54,
    FailureClass.TECHNICAL_DECLINE: 0.26,
    FailureClass.RISK_DECLINE: 0.13,
    FailureClass.MANDATE_REVOKED: 0.07,
}


@dataclass(frozen=True)
class SegmentObservation:
    """One attempt, from the changepoint detector's point of view.

    The detector sees successes as well as failures - a success rate needs a
    denominator - so the generator emits every attempt, not only the failed ones.
    """

    at: datetime
    segment_key: str
    success: bool


@dataclass
class SimulatedBatch:
    """Everything one run produces. Ground truth is held separately, on purpose."""

    scenario: Scenario
    seed: int
    events: list[AtRiskEvent] = field(default_factory=list)
    customers: dict[str, CustomerContext] = field(default_factory=dict)
    latents: LatentStore | None = None
    downtime: DowntimeRegistry = field(default_factory=DowntimeRegistry)
    """What the Downtime API declared. This is all the detection layer may see."""
    all_downtime: DowntimeRegistry = field(default_factory=DowntimeRegistry)
    """Ground truth, declared and undeclared. Evaluation only - never L2."""
    observations: list[SegmentObservation] = field(default_factory=list)
    lifecycle_events: list[tuple[str, str, datetime]] = field(default_factory=list)
    """Observable subscription webhooks: (subscription_id, event_name, occurred_at).

    This is what the mandate FSM is allowed to consume. Building the FSM from
    `true_failure_class` instead would let L2 read the answer key - see
    docs/POSTMORTEM.md D10.
    """

    # --- ground truth, keyed by event id. Never passed to a feature builder. ---
    true_failure_class: dict[str, FailureClass] = field(default_factory=dict)
    next_cycle_at: dict[str, datetime] = field(default_factory=dict)
    segment_state: dict[str, dict[datetime, SegmentHealth]] = field(default_factory=dict)

    # --- descriptive counters, reported by the calibration report --------------
    attempts_total: int = 0
    successes_total: int = 0

    def __len__(self) -> int:
        return len(self.events)

    @property
    def failure_rate(self) -> float:
        return 0.0 if not self.attempts_total else 1 - self.successes_total / self.attempts_total

    def event_by_id(self, event_id: str) -> AtRiskEvent:
        for event in self.events:
            if event.event_id == event_id:
                return event
        raise KeyError(event_id)

    def customer_of(self, event: AtRiskEvent) -> CustomerContext:
        return self.customers[event.customer_id]


class Generator:
    """Builds a `SimulatedBatch`.

    The generative process, in order:

      1. draw customers and assign them to merchants, issuers, and methods
      2. run each issuer x method segment's two-state health chain over the horizon
      3. inject downtime windows on top
      4. walk each mandate's billing cycles, deciding success or failure
      5. for each failure, draw a true cause and emit a plausible error payload
    """

    def __init__(
        self,
        scenario: Scenario | str = "base",
        *,
        seed: int | None = None,
        config: Config | None = None,
    ) -> None:
        self.config = config or get_config()
        self.scenario = get_scenario(scenario) if isinstance(scenario, str) else scenario
        self.seed = seed if seed is not None else int(self.config.get("run.seed"))
        self.sim = self.config.section("simulator")

        start = str(self.sim.get("start_date"))
        self.start = datetime.fromisoformat(start).replace(tzinfo=clock.IST)
        self.horizon_days = int(self.sim.get("horizon_days"))
        self.end = self.start + timedelta(days=self.horizon_days)

    # ------------------------------------------------------------------ build

    def generate(self) -> SimulatedBatch:
        batch = SimulatedBatch(scenario=self.scenario, seed=self.seed)
        batch.latents = LatentStore(self.scenario, self.seed)

        segments = self._segment_keys()
        health = self._run_segment_health(segments)
        batch.segment_state = health
        batch.downtime, batch.all_downtime = self._inject_downtime(segments)

        for customer in self._customers():
            self._run_mandate(batch, customer, health)

        self._generate_checkout_abandonment(batch)

        batch.events.sort(key=lambda e: (e.occurred_at, e.event_id))
        batch.observations.sort(key=lambda o: (o.at, o.segment_key))
        batch.lifecycle_events.sort(key=lambda row: (row[2], row[0]))
        return batch

    # -------------------------------------------------------------- customers

    @dataclass(frozen=True)
    class _Customer:
        customer_id: str
        merchant_id: str
        category: MerchantCategory
        issuer: str
        method: PaymentMethod
        subscription_id: str
        amount_paise: int
        first_cycle_at: datetime

    def _customers(self) -> Iterator[Generator._Customer]:
        n = int(self.sim.get("n_customers"))
        mix = self.sim.get("merchant_mix")
        merchants = [(mid, MerchantCategory(cat), float(share)) for mid, (cat, share) in mix.items()]
        merchant_ids = [m[0] for m in merchants]
        shares = np.array([m[2] for m in merchants], dtype=float)
        shares = shares / shares.sum()
        methods = [PaymentMethod(m) for m in self.sim.get("methods")]

        for index in range(n):
            customer_id = f"cust_{index:06d}"
            rng = substream(self.seed, "customer", customer_id)
            merchant_index = int(rng.choice(len(merchant_ids), p=shares))
            merchant_id, category, _ = merchants[merchant_index]

            amount = self._draw_amount(category, rng)
            # Stagger cycle starts across the month so the batch is not one spike.
            offset_days = int(rng.integers(0, 30))
            hour = int(rng.integers(0, 24))
            minute = int(rng.integers(0, 60))

            yield Generator._Customer(
                customer_id=customer_id,
                merchant_id=merchant_id,
                category=category,
                issuer=str(rng.choice(ISSUERS)),
                method=methods[int(rng.integers(0, len(methods)))],
                subscription_id=deterministic_id("sub", customer_id),
                amount_paise=amount,
                first_cycle_at=self.start + timedelta(days=offset_days, hours=hour, minutes=minute),
            )

    def _draw_amount(self, category: MerchantCategory, rng: np.random.Generator) -> int:
        """LogNormal, truncated, per category.

        Parameterised so both sides of the AFA boundary are exercised - the
        discontinuity at Rs 15,000, or Rs 1,00,000 for the three high-ceiling
        categories, is decision-relevant and the evaluation has to hit it.
        SIMULATOR_CARD section 4.3.
        """
        profiles = self.sim.get("amount.by_category")
        params = profiles.get(category.value, profiles["DEFAULT"])
        rupees = float(rng.lognormal(mean=params["mu"], sigma=params["sigma"]))
        rupees = min(max(rupees, params["min_rupees"]), params["max_rupees"])
        return round(rupees) * 100

    # ---------------------------------------------------------- segment health

    def _segment_keys(self) -> list[str]:
        methods = [PaymentMethod(m) for m in self.sim.get("methods")]
        return [f"{issuer}:{method.value}" for issuer in ISSUERS for method in methods]

    def _run_segment_health(self, segments: list[str]) -> dict[str, dict[datetime, SegmentHealth]]:
        """Two-state Markov chain per segment, evaluated daily.

        This is what `antar/detect/changepoint.py` has to find. The generator knows
        the true state; the detector only ever sees the success rate.
        """
        p_down = float(self.sim.get("segment_health.p_healthy_to_degraded"))
        p_up = float(self.sim.get("segment_health.p_degraded_to_healthy"))
        out: dict[str, dict[datetime, SegmentHealth]] = {}

        for segment in segments:
            rng = substream(self.seed, "segment_health", segment)
            state = SegmentHealth.HEALTHY
            timeline: dict[datetime, SegmentHealth] = {}
            for day in range(self.horizon_days + 1):
                when = (self.start + timedelta(days=day)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if state is SegmentHealth.HEALTHY and rng.random() < p_down:
                    state = SegmentHealth.DEGRADED
                elif state is SegmentHealth.DEGRADED and rng.random() < p_up:
                    state = SegmentHealth.HEALTHY
                timeline[when] = state
            out[segment] = timeline
        return out

    def _health_on(
        self, health: dict[str, dict[datetime, SegmentHealth]], segment: str, when: datetime
    ) -> SegmentHealth:
        day = when.replace(hour=0, minute=0, second=0, microsecond=0)
        return health.get(segment, {}).get(day, SegmentHealth.HEALTHY)

    # ---------------------------------------------------------------- downtime

    def _inject_downtime(self, segments: list[str]) -> tuple[DowntimeRegistry, DowntimeRegistry]:
        """Returns `(declared, all)`.

        `declared` is what Antar's detection layer gets to see - the Downtime API's
        view. `all` is the truth, used to decide whether a debit fails and to grade
        the detector afterwards.

        Keeping them apart is the anti-circularity guard for the detection result.
        Handing the detector the same registry the generator wrote would produce an
        `ISSUER_DOWN` recall near 1.00 that measures our bookkeeping rather than the
        detector. The undeclared remainder is exactly what the changepoint detector
        exists to find.
        """
        rate = float(self.sim.get("downtime.windows_per_100_days"))
        rate *= self.scenario.downtime_multiplier
        expected = rate * self.horizon_days / 100.0
        durations = self.sim.get("downtime.duration_hours")
        severity_mix = self.sim.get("downtime.severity_mix")
        declared_share = float(self.sim.get("downtime.declared_share"))
        severities = [DowntimeSeverity(s) for s in severity_mix]
        weights = np.array(list(severity_mix.values()), dtype=float)
        weights = weights / weights.sum()

        declared = DowntimeRegistry()
        everything = DowntimeRegistry()
        if expected <= 0:
            return declared, everything

        for segment in segments:
            rng = substream(self.seed, "downtime", segment)
            count = int(rng.poisson(expected))
            issuer, method_name = segment.split(":")
            for index in range(count):
                severity = severities[int(rng.choice(len(severities), p=weights))]
                begin = self.start + timedelta(
                    minutes=float(rng.uniform(0, self.horizon_days * 24 * 60))
                )
                hours = float(durations[severity.value]) * float(rng.uniform(0.6, 1.6))
                end = begin + timedelta(hours=hours)
                is_declared = bool(rng.random() < declared_share)
                window = DowntimeWindow(
                    downtime_id=deterministic_id("down", segment, index),
                    method=PaymentMethod(method_name),
                    issuer=issuer,
                    severity=severity,
                    status=(
                        DowntimeStatus.RESOLVED if end < self.end else DowntimeStatus.STARTED
                    ),
                    begin=begin,
                    end=end,
                )
                everything.upsert(window)
                if is_declared:
                    declared.upsert(window)
        return declared, everything

    # ----------------------------------------------------------------- cycles

    def _run_mandate(
        self,
        batch: SimulatedBatch,
        customer: Generator._Customer,
        health: dict[str, dict[datetime, SegmentHealth]],
    ) -> None:
        assert batch.latents is not None
        latents = batch.latents.get(customer.customer_id)
        segment = f"{customer.issuer}:{customer.method.value}"
        ceiling = self._afa_ceiling(customer.category)
        state = MandateState.ACTIVE
        cycles = int(self.sim.get("cycles_per_mandate"))

        batch.customers[customer.customer_id] = CustomerContext(
            customer_id=customer.customer_id,
            merchant_id=customer.merchant_id,
            mandate_state=state,
            # Taken from the latents, not drawn separately. Tenure is one of the very
            # few quantities in the latent vector that a merchant genuinely observes,
            # so the customer record and the truth must be the same number. An
            # earlier version drew them from two substreams, which left the only
            # observable driver of persuasion uncorrelated with the driver itself and
            # quietly destroyed a real signal. See docs/POSTMORTEM.md D7.
            tenure_months=latents.tenure_months,
        )

        for cycle in range(1, cycles + 1):
            charge_at = customer.first_cycle_at + timedelta(days=30 * (cycle - 1))
            if charge_at >= self.end or state is not MandateState.ACTIVE:
                break

            rng = substream(self.seed, "cycle", customer.customer_id, cycle)
            segment_health = self._health_on(health, segment, charge_at)
            # Failure is decided by the *truth*, declared or not.
            outage = batch.all_downtime.overlap_for(charge_at, customer.method, customer.issuer)

            p_fail = self._failure_probability(
                latents_balance=latents.balance_fraction(charge_at),
                segment_health=segment_health,
                outage=outage,
                amount_paise=customer.amount_paise,
                ceiling=ceiling,
            )
            failed = bool(rng.random() < p_fail)

            batch.attempts_total += 1
            batch.successes_total += int(not failed)
            batch.observations.append(
                SegmentObservation(at=charge_at, segment_key=segment, success=not failed)
            )
            if not failed:
                continue

            cause = self._draw_cause(
                rng,
                outage=outage,
                amount_paise=customer.amount_paise,
                ceiling=ceiling,
                cycle=cycle,
            )
            error = failure_emission.emit(cause, rng)
            event = AtRiskEvent(
                event_id=make_event_id(
                    customer.merchant_id, customer.customer_id, customer.subscription_id, cycle
                ),
                merchant_id=customer.merchant_id,
                customer_id=customer.customer_id,
                loss_class=LossClass.MANDATE_FAILURE,
                subscription_id=customer.subscription_id,
                cycle_number=cycle,
                amount_paise=customer.amount_paise,
                merchant_category=customer.category,
                method=customer.method,
                issuer=customer.issuer,
                occurred_at=charge_at,
                error_code=error.code,
                error_reason=error.reason,
                error_source=error.source,
                error_step=error.step,
                error_description=error.description,
                raw={"simulated": True, "segment": segment},
            )
            batch.events.append(event)
            batch.true_failure_class[event.event_id] = cause
            # The next scheduled debit: 30 days out, the horizon over which recovery
            # is attributed and the instant the balance curve is evaluated at.
            batch.next_cycle_at[event.event_id] = charge_at + timedelta(days=30)

            if cause is FailureClass.MANDATE_REVOKED:
                # The webhook Razorpay would send. Timestamped at the failure, so a
                # detector asking "what was the state when this failed?" correctly
                # gets ACTIVE - we learn of the revocation from this event, not
                # before it.
                batch.lifecycle_events.append(
                    (customer.subscription_id, "subscription.cancelled", charge_at)
                )
                state = MandateState.REVOKED
                batch.customers[customer.customer_id] = batch.customers[
                    customer.customer_id
                ].model_copy(update={"mandate_state": state})

    def _afa_ceiling(self, category: MerchantCategory) -> int:
        ceilings = self.config.get("policy.afa_free_ceiling_paise")
        return int(ceilings.get(category.value, ceilings["DEFAULT"]))

    def _failure_probability(
        self,
        *,
        latents_balance: float,
        segment_health: SegmentHealth,
        outage: DowntimeWindow | None,
        amount_paise: int,
        ceiling: int,
    ) -> float:
        if outage is not None:
            # A high-severity window is near-total failure for that segment.
            return float(self.sim.get(f"downtime.fail_probability.{outage.severity.value}"))

        p = self.scenario.base_failure_rate
        # Low balance drives failure. This is the mechanism behind
        # INSUFFICIENT_FUNDS being the dominant cause and behind rescheduling
        # toward the salary peak being worth anything.
        p *= 0.45 + 1.35 * (1.0 - latents_balance)
        if segment_health is SegmentHealth.DEGRADED:
            p *= float(self.sim.get("segment_health.degraded_fail_multiplier"))
        if amount_paise > ceiling:
            # Above the AFA-free ceiling the debit needs an authentication step the
            # customer has to complete, and many do not.
            p = 1.0 - (1.0 - p) * 0.55
        return float(min(max(p, 0.0), 0.98))

    def _draw_cause(
        self,
        rng: np.random.Generator,
        *,
        outage: DowntimeWindow | None,
        amount_paise: int,
        ceiling: int,
        cycle: int,
    ) -> FailureClass:
        if outage is not None:
            return FailureClass.ISSUER_DOWN
        if amount_paise > ceiling and rng.random() < AFA_SHARE_ABOVE_CEILING:
            # Above the category threshold RBI-EM-03/-04 require an additional factor
            # each time, and most failures there are the customer not completing it.
            # Not *all* of them, though: a Rs 40,000 debit can still bounce for want
            # of funds. Making this deterministic would have handed the detector a
            # free 100% on the AFA class - it could infer the label from the amount
            # alone - and a per-class recall of 1.00 that a reviewer sees through
            # instantly is worse than an honest 0.8.
            return FailureClass.AFA_REQUIRED

        classes = list(BASE_CAUSE_SHARES)
        weights = np.array([BASE_CAUSE_SHARES[c] for c in classes], dtype=float)
        # Revocation becomes more likely as a mandate ages, which is the only
        # cycle-dependence in the cause model.
        revoked_index = classes.index(FailureClass.MANDATE_REVOKED)
        weights[revoked_index] *= 1.0 + 0.25 * (cycle - 1)
        weights = weights / weights.sum()
        return classes[int(rng.choice(len(classes), p=weights))]

    # ------------------------------------------------- checkout abandonment

    def _generate_checkout_abandonment(self, batch: SimulatedBatch) -> None:
        """The second loss class (T1 tier). Config-driven, not a second codebase."""
        count = int(self.sim.get("checkout_abandon_events"))
        if count <= 0:
            return
        assert batch.latents is not None
        methods = [PaymentMethod(m) for m in self.sim.get("methods")]
        mix = self.sim.get("merchant_mix")
        merchants = [(mid, MerchantCategory(cat)) for mid, (cat, _share) in mix.items()]

        for index in range(count):
            abandon_id = f"abn_{index:06d}"
            rng = substream(self.seed, "abandon", abandon_id)
            merchant_id, category = merchants[int(rng.integers(0, len(merchants)))]
            customer_id = f"cust_{int(rng.integers(0, int(self.sim.get('n_customers')))):06d}"
            latents = batch.latents.get(customer_id)
            occurred = self.start + timedelta(
                minutes=float(rng.uniform(0, self.horizon_days * 24 * 60))
            )
            amount = self._draw_amount(category, rng)
            error = failure_emission.emit(FailureClass.TECHNICAL_DECLINE, rng)

            event = AtRiskEvent(
                event_id=deterministic_id("evt", abandon_id),
                merchant_id=merchant_id,
                customer_id=customer_id,
                loss_class=LossClass.CHECKOUT_ABANDON,
                subscription_id=None,
                cycle_number=None,
                amount_paise=amount,
                merchant_category=category,
                method=methods[int(rng.integers(0, len(methods)))],
                issuer=str(rng.choice(ISSUERS)),
                occurred_at=occurred,
                error_code=error.code,
                error_reason=error.reason,
                error_source=error.source,
                error_step=error.step,
                error_description=error.description,
                raw={"simulated": True, "loss_class": "CHECKOUT_ABANDON"},
            )
            batch.events.append(event)
            batch.true_failure_class[event.event_id] = FailureClass.TECHNICAL_DECLINE
            batch.next_cycle_at[event.event_id] = occurred + timedelta(days=3)
            batch.customers.setdefault(
                customer_id,
                CustomerContext(
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    tenure_months=latents.tenure_months,
                ),
            )



def generate(
    scenario: Scenario | str = "base",
    *,
    seed: int | None = None,
    config: Config | None = None,
) -> SimulatedBatch:
    """Convenience entry point."""
    return Generator(scenario, seed=seed, config=config).generate()


def batch_summary(batch: SimulatedBatch) -> dict[str, Any]:
    """Descriptive counts for the calibration report and the simulator card.

    Every `[MEASURE @ M2]` placeholder in docs/SIMULATOR_CARD.md is filled from
    here, by script, never by hand.
    """
    from collections import Counter

    loss = Counter(e.loss_class.value for e in batch.events)
    # Reported per loss class. Pooling them would make the checkout-abandonment
    # stream, which is TECHNICAL_DECLINE by construction, swamp the mandate-failure
    # mix and misrepresent both.
    mandate_ids = {e.event_id for e in batch.events if e.loss_class is LossClass.MANDATE_FAILURE}
    mandate_classes = Counter(
        cls for eid, cls in batch.true_failure_class.items() if eid in mandate_ids
    )
    total = sum(mandate_classes.values()) or 1

    return {
        "scenario": batch.scenario.name,
        "seed": batch.seed,
        "customers": len(batch.customers),
        "attempts": batch.attempts_total,
        "successes": batch.successes_total,
        "first_attempt_failure_rate": round(batch.failure_rate, 4),
        "at_risk_events": len(batch.events),
        "mandate_failures": loss.get("MANDATE_FAILURE", 0),
        "checkout_abandons": loss.get("CHECKOUT_ABANDON", 0),
        "downtime_windows": len(batch.all_downtime),
        "downtime_windows_declared": len(batch.downtime),
        "mandate_failure_class_shares": {
            cls.value: round(count / total, 4) for cls, count in sorted(mandate_classes.items())
        },
        "at_risk_value_paise": sum(e.amount_paise for e in batch.events),
        "above_afa_ceiling": sum(1 for e in batch.events if _above_ceiling(e)),
        "above_afa_ceiling_share": round(
            sum(1 for e in batch.events if _above_ceiling(e)) / max(len(batch.events), 1), 4
        ),
    }


def _above_ceiling(event: AtRiskEvent) -> bool:
    """RBI-EM-03 / RBI-EM-04 boundary, using the statutory defaults.

    Hard-coded here and only here, for a descriptive counter. The values that
    business logic uses live in `antar/policy/regulations.py` as data with citations
    (N5); this is a reporting convenience and is not consulted by any decision.
    """
    ceiling = 10_000_000 if event.merchant_category in HIGH_CEILING_CATEGORIES else 1_500_000
    return event.amount_paise > ceiling
