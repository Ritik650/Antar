"""Every generated error code must exist in the documented Razorpay taxonomy.

docs/SIMULATOR_CARD.md section 10. A fabricated error code in a fixture is
indistinguishable from a real one to a reviewer, so the generator is not allowed to
invent vocabulary. The *shares* are ours; the *labels* are not.

This also checks the property that makes detection a real problem rather than a
lookup: several reasons are emitted by more than one true cause. If that overlap ever
disappeared, the detector's precision would become an artefact of the generator
inverting its own table.
"""

from __future__ import annotations

import pytest

from antar.signals import razorpay_errors as rz
from antar.signals.schemas import FailureClass
from antar.simulator import failure_emission
from antar.simulator.scenarios import REPORTED
from tests.statistical.helpers import batch as cached_batch

pytestmark = pytest.mark.statistical


@pytest.mark.parametrize("scenario", REPORTED)
def test_every_generated_error_code_is_documented(scenario):
    batch = cached_batch(scenario)
    assert batch.events, "empty batch"
    for event in batch.events:
        assert event.error_reason in rz.DOCUMENTED_REASONS, (
            f"{event.event_id} emitted undocumented reason {event.error_reason!r}"
        )
        assert event.error_code in rz.DOCUMENTED_CODES
        assert event.error_source in rz.DOCUMENTED_SOURCES
        assert event.error_step in rz.DOCUMENTED_STEPS


def test_the_error_quadruple_is_internally_consistent():
    """code / reason / source / step come from one catalogue row, not four draws."""
    batch = cached_batch()
    for event in batch.events:
        entry = rz.BY_REASON[event.error_reason]
        assert (event.error_code, event.error_source, event.error_step) == (
            entry.code,
            entry.source,
            entry.step,
        ), f"{event.event_id} has a spliced error payload"


def test_emission_shares_are_a_valid_distribution():
    for failure_class, table in failure_emission.EMISSION.items():
        assert abs(sum(table.values()) - 1.0) < 1e-9, failure_class
        assert all(0.0 <= share <= 1.0 for share in table.values())


def test_emission_covers_every_failure_class_the_generator_can_produce():
    producible = set(failure_emission.EMISSION)
    assert FailureClass.UNKNOWN not in producible, (
        "UNKNOWN is a detector output, never a generated ground truth"
    )
    for failure_class in FailureClass:
        if failure_class is FailureClass.UNKNOWN:
            continue
        assert failure_class in producible, f"no emission table for {failure_class}"


def test_error_codes_overlap_across_causes():
    """The property that stops detection from being circular.

    If the generator emitted a unique code per cause, a lookup table would score
    100% and the classifier, the downtime cross-check, and the changepoint detector
    would all be decoration.
    """
    ambiguous = failure_emission.ambiguous_reasons()
    assert len(ambiguous) >= 4, f"too little overlap for detection to be non-trivial: {ambiguous}"
    # The specific ones the architecture depends on being hard.
    assert "gateway_technical_error" in ambiguous, (
        "ISSUER_DOWN and TECHNICAL_DECLINE must share a code, or the downtime "
        "cross-check has nothing to contribute"
    )
    assert "declined_by_issuer" in ambiguous
    assert "payment_failed" in ambiguous


def test_a_meaningful_share_of_generated_events_carries_an_ambiguous_code():
    """Overlap in the table is not enough; it has to show up in the data."""
    batch = cached_batch()
    ambiguous = failure_emission.ambiguous_reasons()
    share = sum(1 for e in batch.events if e.error_reason in ambiguous) / len(batch.events)
    assert share > 0.10, (
        f"only {share:.1%} of events carry an ambiguous code; detection would be "
        "close to a lookup"
    )


def test_the_catalogue_is_fully_exercised():
    """Every documented row should be reachable, or it is dead weight in a fixture
    directory that claims to cover what we consume."""
    emitted = {reason for table in failure_emission.EMISSION.values() for reason in table}
    unreachable = rz.DOCUMENTED_REASONS - emitted
    assert not unreachable, f"documented reasons the generator can never emit: {sorted(unreachable)}"
