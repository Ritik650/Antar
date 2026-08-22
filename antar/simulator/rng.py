"""Deterministic random substreams.

A fixed seed must produce a byte-identical event stream (PLAN.md 9.3). That is
harder than passing one `Generator` around: any change to iteration order, any
added draw in an unrelated branch, shifts every subsequent number and silently
invalidates every cross-run comparison.

So no component shares a stream. Each draws from a substream derived from
`(seed, purpose, key...)` by hash. Adding a new draw to the failure model cannot
perturb the customer latents, and generating customer 900 alone gives the same
numbers as generating all 2,000 and taking the 900th.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np


def substream_seed(seed: int, purpose: str, *keys: Any) -> int:
    """A 64-bit seed that is a pure function of the base seed and the key path."""
    payload = "\x1f".join([str(seed), purpose, *(str(k) for k in keys)])
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def substream(seed: int, purpose: str, *keys: Any) -> np.random.Generator:
    return np.random.default_rng(substream_seed(seed, purpose, *keys))


def beta_from_mean(mean: float, concentration: float) -> tuple[float, float]:
    """Beta parameters for a given mean and concentration (alpha + beta).

    Scenarios are specified by the quantity that has meaning - the mean self-heal
    rate, the mean opt-out sensitivity - rather than by shape parameters nobody can
    reason about. Concentration controls dispersion, which is the heterogeneity knob.
    """
    if not 0.0 < mean < 1.0:
        raise ValueError(f"mean must be in (0, 1), got {mean}")
    if concentration <= 0:
        raise ValueError(f"concentration must be positive, got {concentration}")
    return mean * concentration, (1.0 - mean) * concentration
