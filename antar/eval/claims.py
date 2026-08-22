"""Claim gating: what the artifacts are allowed to say.

docs/SIMULATOR_CARD.md section 10 gives each validation test a *consequence* rather
than just a pass/fail. For the anti-circularity test the consequence is specific:

> Sleeping-dogs finding is withdrawn from all artifacts.

That is a statement about the README, the console, and the pitch video, not about
the test runner. So the gate lives here as a value the artifacts read, rather than as
an assertion buried in a test file. `scripts/run_evaluation.py` writes the verdict to
`artifacts/claims.json`, the README generator refuses to state a withdrawn claim, and
`tests/statistical/test_anti_circularity.py` asserts the two agree.

The effect is that a claim cannot outlive the evidence for it. If someone changes a
simulator parameter and the negative-uplift population disappears, the README stops
asserting it on the next `make evaluate` - it does not quietly keep the old sentence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from antar import clock
from antar.ids import intervention_id
from antar.signals.schemas import (
    CONTACT_CHANNELS,
    FailureClass,
    Intervention,
    MessageClass,
)
from antar.simulator.latents import LatentStore
from antar.simulator.response_model import ResponseModel
from antar.simulator.scenarios import REPORTED, get_scenario

# Pre-registered before the first measurement. A population smaller than this is
# noise rather than a finding, and the finding must appear in at least
# MIN_QUALIFYING_SCENARIOS of the three.
NON_TRIVIAL_SHARE = 0.05
MIN_QUALIFYING_SCENARIOS = 2

# The amount at which uplift is evaluated for the population scan. Held fixed so the
# share is comparable across scenarios rather than confounded with the amount mix.
SCAN_AMOUNT_PAISE = 49_900
SCAN_CUSTOMERS = 1500

SCAN_REFERENCE = datetime(2026, 4, 28, 11, 4, tzinfo=clock.IST)
"""The instant the population scan is evaluated at. **Fixed, never `clock.now()`.**

This scan is a property of the simulator's parameters, not of today's date. An earlier
version read the authoritative clock, and the consequence was that the reported
negative-uplift share in the base scenario moved from 5.13% to 4.67% overnight - across
a midnight, with no code change - because `next_balance_peak` landed on a different day
of the month and a handful of customers crossed zero. A pre-registered threshold that a
calendar day can flip is not a measurement.

The date is inside the simulated horizon and is the same instant `tests/conftest.py`
freezes to, so a scan run under test and a scan run by `make evaluate` agree."""


@dataclass(frozen=True)
class ClaimVerdict:
    """Whether one claim survived its test, and the number behind the answer."""

    claim: str
    supported: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def best_available_uplift(
    model: ResponseModel, latents: Any, *, amount_paise: int = SCAN_AMOUNT_PAISE
) -> float:
    """Ground-truth uplift under the action most favourable to treatment.

    The maximum over every contactable channel, timed at the customer's balance
    peak. Taking the best case is deliberate: it removes "you picked the wrong
    channel" and "you asked at the wrong time" as explanations, so a customer who is
    still negative here is negative under any action Antar could have chosen.
    """
    base = SCAN_REFERENCE
    next_cycle = base + timedelta(days=30)
    # RBI-EM-01 puts a 24-hour floor under any scheduled action, so the earliest
    # admissible peak is the first one at least a day out.
    peak = latents.next_balance_peak(base + timedelta(hours=24))

    best = -1.0
    for channel in sorted(CONTACT_CHANNELS, key=lambda c: c.value):
        candidate = Intervention(
            intervention_id=intervention_id(latents.customer_id, channel, peak, 0),
            event_id=f"scan_{latents.customer_id}",
            channel=channel,
            message_class=MessageClass.TRANSACTIONAL,
            scheduled_for=peak,
        )
        truth = model.evaluate(
            latents,
            failure_class=FailureClass.INSUFFICIENT_FUNDS,
            amount_paise=amount_paise,
            next_cycle_at=next_cycle,
            intervention=candidate,
        )
        best = max(best, truth.uplift)
    return best


def negative_uplift_share(
    scenario_name: str, *, seed: int, n_customers: int = SCAN_CUSTOMERS
) -> float:
    """Share of customers whose best available action still has negative uplift."""
    scenario = get_scenario(scenario_name)
    model = ResponseModel(scenario, seed)
    store = LatentStore(scenario, seed)
    uplifts = np.array(
        [
            best_available_uplift(model, store.get(f"cust_{index:06d}"))
            for index in range(n_customers)
        ]
    )
    return float((uplifts < 0).mean())


def sleeping_dogs_verdict(
    *, seeds: tuple[int, ...] = (20260822,), n_customers: int = SCAN_CUSTOMERS
) -> ClaimVerdict:
    """Does the negative-uplift population survive its pre-registered test?"""
    shares: dict[str, list[float]] = {
        name: [negative_uplift_share(name, seed=s, n_customers=n_customers) for s in seeds]
        for name in REPORTED
    }
    means = {name: float(np.mean(values)) for name, values in shares.items()}
    qualifying = sorted(name for name, mean in means.items() if mean >= NON_TRIVIAL_SHARE)
    supported = len(qualifying) >= MIN_QUALIFYING_SCENARIOS

    if supported:
        detail = (
            f"Negative-uplift mass of at least {NON_TRIVIAL_SHARE:.0%} appears in "
            f"{len(qualifying)} of {len(REPORTED)} scenarios ({', '.join(qualifying)}). "
            "The claim is supported, in simulation."
        )
    else:
        detail = (
            f"Negative-uplift mass reaches {NON_TRIVIAL_SHARE:.0%} in only "
            f"{len(qualifying)} scenario(s). Per SIMULATOR_CARD section 10 the "
            "sleeping-dogs finding is WITHDRAWN from all artifacts."
        )

    return ClaimVerdict(
        claim="sleeping_dogs",
        supported=supported,
        detail=detail,
        evidence={
            "threshold": NON_TRIVIAL_SHARE,
            "min_qualifying_scenarios": MIN_QUALIFYING_SCENARIOS,
            "seeds": list(seeds),
            "share_by_scenario": {k: round(v, 4) for k, v in means.items()},
            "share_by_scenario_by_seed": {
                k: [round(v, 4) for v in values] for k, values in shares.items()
            },
            "qualifying_scenarios": qualifying,
            "margin_above_threshold": {
                k: round(v - NON_TRIVIAL_SHARE, 4) for k, v in means.items()
            },
        },
    )


def write_claims(verdicts: list[ClaimVerdict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({v.claim: v.as_dict() for v in verdicts}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_claims(path: Path) -> dict[str, ClaimVerdict]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: ClaimVerdict(
            claim=value["claim"],
            supported=bool(value["supported"]),
            detail=value["detail"],
            evidence=value.get("evidence", {}),
        )
        for key, value in raw.items()
    }
