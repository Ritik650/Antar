"""Shared, cached fixtures for the statistical gate.

Generating a batch takes a few seconds, and the gate calls for the same
(scenario, seed) combinations repeatedly. Caching keeps the suite fast enough that
people actually run it, which matters more for these tests than for any others: a
statistical gate nobody runs is a comment.

Caching is safe here precisely because of `test_determinism` - a batch is a pure
function of (scenario, seed), so a cached one is indistinguishable from a fresh one.
Every batch is returned read-only by convention; no test mutates one.
"""

from __future__ import annotations

from functools import lru_cache

from antar.simulator.generator import SimulatedBatch, generate

DEFAULT_SEED = 20260822


@lru_cache(maxsize=12)
def batch(scenario: str = "base", seed: int = DEFAULT_SEED) -> SimulatedBatch:
    return generate(scenario, seed=seed)
