"""Fixed seed -> byte-identical event stream.

PLAN.md section 9.3 makes nondeterminism a bug until proven otherwise. Without this
every cross-run comparison in the evaluation is meaningless: a policy that looks
better might simply have been generated on a different roll.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from antar.simulator.generator import generate
from antar.simulator.latents import draw_latents
from antar.simulator.scenarios import BASE, REPORTED, get_scenario

pytestmark = pytest.mark.statistical


def stream_digest(batch) -> str:
    """A hash over the whole event stream, field for field."""
    payload = [
        [
            e.event_id, e.merchant_id, e.customer_id, e.loss_class.value, e.subscription_id,
            e.cycle_number, e.amount_paise, e.merchant_category.value, e.method.value, e.issuer,
            e.occurred_at.isoformat(), e.error_code, e.error_reason, e.error_source, e.error_step,
        ]
        for e in batch.events
    ]
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("scenario", REPORTED)
def test_same_seed_gives_an_identical_stream(scenario):
    first = generate(scenario, seed=20260822)
    second = generate(scenario, seed=20260822)
    assert stream_digest(first) == stream_digest(second)
    assert len(first.events) == len(second.events)


def test_different_seed_gives_a_different_stream():
    """A determinism test that passes for a constant generator proves nothing."""
    assert stream_digest(generate("base", seed=1)) != stream_digest(generate("base", seed=2))


def test_different_scenario_gives_a_different_stream():
    assert stream_digest(generate("base", seed=7)) != stream_digest(
        generate("conservative", seed=7)
    )


def test_latents_are_independent_of_generation_order():
    """Substream keying, not a shared generator.

    Customer 900's latents must be the same whether we draw ten customers or ten
    thousand. If they are not, adding a draw anywhere silently rewrites history.
    """
    direct = draw_latents("cust_000900", BASE, 20260822)
    after_others = None
    for index in range(0, 1000, 97):
        drawn = draw_latents(f"cust_{index:06d}", BASE, 20260822)
        if drawn.customer_id == "cust_000900":  # pragma: no cover - defensive
            after_others = drawn
    _ = after_others
    again = draw_latents("cust_000900", BASE, 20260822)
    assert direct == again


def test_downtime_windows_are_deterministic():
    a = generate("base", seed=20260822)
    b = generate("base", seed=20260822)
    ids_a = sorted((w.downtime_id, w.begin.isoformat(), w.severity.value) for w in a.downtime)
    ids_b = sorted((w.downtime_id, w.begin.isoformat(), w.severity.value) for w in b.downtime)
    assert ids_a == ids_b


def test_ground_truth_is_deterministic():
    a = generate("base", seed=20260822)
    b = generate("base", seed=20260822)
    assert a.true_failure_class == b.true_failure_class
    assert a.next_cycle_at == b.next_cycle_at


def test_scenario_parameters_are_stable_values_not_drawn():
    """Scenario definitions must not contain randomness of their own."""
    for name in REPORTED:
        assert get_scenario(name).as_dict() == get_scenario(name).as_dict()
