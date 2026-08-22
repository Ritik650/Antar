"""Segment health: EWMA and CUSUM on the success rate per issuer x method.

**No LLM.** See `tests/unit/test_no_llm_in_detection.py`.

## What this is for

A failure tells you almost nothing on its own. A failure in a segment whose success
rate has quietly halved over the last four hours tells you a great deal - it says the
problem is the rail, not the customer, and that contacting anybody in that segment is
spending a TRAI contact slot and an RBI-EM-01 notification on a debit that was never
going to succeed.

The Downtime API covers outages Razorpay knows about. This covers the ones nobody has
declared yet, which are the expensive ones.

## Why both EWMA and CUSUM

They answer different questions and disagree usefully.

  * **EWMA** is a smoothed estimate of the current success rate. Good for "how bad is
    it right now", poor at detecting a small sustained shift, because a small shift
    never moves it far from the baseline.
  * **CUSUM** accumulates deviation, so a shift of half a standard deviation that
    persists will eventually cross the threshold even though the EWMA barely moved.
    It is the right tool for the slow degradation that costs the most money, because
    nobody notices it.

CUSUM decides the state; EWMA is reported alongside so a human looking at the console
sees a number they can interpret.

## The tuning tradeoff, stated plainly

`cusum_h` trades detection delay against false alarms. A false alarm makes Antar
*wait* when it could have acted, which costs the delay on a recoverable cycle. A
missed detection makes Antar *act* into an outage, which costs an attempt, a
notification, and the opt-out hazard that notification carries. The second is worse,
so the defaults are set to detect early and tolerate false alarms.
`scripts/changepoint_report.py` measures both against injected downtime, and the
numbers go in the README rather than the tuning being asserted.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from antar.signals.schemas import SegmentHealth, SegmentHealthReport


@dataclass
class SegmentTracker:
    """Online EWMA + one-sided CUSUM for one segment's success rate."""

    segment_key: str
    ewma_alpha: float = 0.20
    cusum_k: float = 0.5
    cusum_h: float = 4.0
    min_observations: int = 20
    degrading_threshold: float = 2.0
    degraded_threshold: float = 4.0
    recovery_observations: int = 10

    # --- state ---------------------------------------------------------------
    baseline_window: int = 200
    """How much recent history the baseline is re-estimated from after a recovery.

    Long, on purpose. An earlier version re-estimated it from the EWMA, which with
    alpha=0.2 is effectively a five-observation average; one lucky window set the
    baseline to 0.967 on a rail whose true rate was 0.75, after which the CUSUM
    drifted upward forever and the segment was stuck DEGRADED for the rest of the
    horizon. POSTMORTEM D12.
    """

    n: int = 0
    successes: int = 0
    ewma: float = field(default=1.0)
    baseline: float = field(default=1.0)
    cusum: float = 0.0
    state: SegmentHealth = SegmentHealth.HEALTHY
    since_recovery: int = 0
    last_at: datetime | None = None
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    @property
    def warm(self) -> bool:
        """Enough history for the baseline to mean anything."""
        return self.n >= self.min_observations

    @property
    def sigma(self) -> float:
        """Bernoulli standard deviation at the baseline rate.

        Floored well above zero. A segment whose warm-up happened to contain no
        failures at all has an empirical baseline of exactly 1.0 and a true sigma of
        0, which would make its very first failure an infinitely large deviation.
        Laplace smoothing (below) keeps the baseline off the boundary; this floor
        catches what is left.
        """
        p = min(max(self.baseline, 1e-6), 1 - 1e-6)
        return max(math.sqrt(p * (1.0 - p)), 0.15)

    def observe(self, success: bool, at: datetime) -> SegmentHealth:
        """Fold in one attempt and return the resulting state."""
        value = 1.0 if success else 0.0
        self.n += 1
        self.successes += int(success)
        self.last_at = at
        if self.recent.maxlen != self.baseline_window:
            self.recent = deque(self.recent, maxlen=self.baseline_window)
        self.recent.append(value)

        if not self.warm:
            # Learn the baseline before judging anything against it. During warm-up
            # the EWMA tracks the running mean so it starts somewhere sensible.
            #
            # Laplace-smoothed - a Beta(1,1) prior - rather than the raw empirical
            # mean. A warm-up that happens to contain no failures would otherwise
            # give a baseline of exactly 1.0, a true sigma of 0, and an alarm on the
            # segment's very first failure. The prior costs a little sensitivity on
            # genuinely perfect rails and buys immunity to a degenerate start.
            self.baseline = (self.successes + 1.0) / (self.n + 2.0)
            self.ewma = self.baseline
            self.state = SegmentHealth.HEALTHY
            return self.state

        self.ewma = self.ewma_alpha * value + (1 - self.ewma_alpha) * self.ewma

        # One-sided lower CUSUM on the **standardised** deviation. Both the
        # allowance `k` and the thresholds are in units of sigma, so they have to be
        # compared against a standardised increment. An earlier version accumulated
        # raw probability deviations and compared them against sigma-scaled
        # thresholds, which made two consecutive failures enough to declare a
        # segment degrading and produced an 80% false-alarm rate. POSTMORTEM D8.
        z = (self.baseline - value) / self.sigma
        # Capped as well as floored. An unbounded CUSUM that has drifted high takes
        # proportionally longer to come back, so a transient cannot lock the segment
        # out for the rest of the horizon.
        self.cusum = min(
            max(0.0, self.cusum + z - self.cusum_k), 3.0 * self.degraded_threshold
        )

        return self._transition()

    def _transition(self) -> SegmentHealth:
        scaled = self.cusum
        previous = self.state

        if scaled >= self.degraded_threshold:
            self.state = SegmentHealth.DEGRADED
            self.since_recovery = 0
        elif scaled >= self.degrading_threshold:
            # Never step back from DEGRADED to DEGRADING; that reads as improvement
            # when it is only noise. Recovery goes through RECOVERING.
            self.state = (
                SegmentHealth.DEGRADED
                if previous is SegmentHealth.DEGRADED
                else SegmentHealth.DEGRADING
            )
            self.since_recovery = 0
        elif previous in (SegmentHealth.DEGRADED, SegmentHealth.DEGRADING):
            self.state = SegmentHealth.RECOVERING
            self.since_recovery = 1
        elif previous is SegmentHealth.RECOVERING:
            self.since_recovery += 1
            if self.since_recovery >= self.recovery_observations:
                # Sustained normality. Re-estimate the baseline from a long window of
                # recent history, so a permanently worse rail becomes the new normal
                # rather than alarming forever - but from enough observations that a
                # lucky run cannot poison it. POSTMORTEM D12.
                self.state = SegmentHealth.HEALTHY
                self.baseline = self._recent_rate()
                self.cusum = 0.0
        else:
            self.state = SegmentHealth.HEALTHY

        return self.state

    def _recent_rate(self) -> float:
        """Laplace-smoothed success rate over the recent window."""
        if not self.recent:
            return self.baseline
        return (sum(self.recent) + 1.0) / (len(self.recent) + 2.0)

    def report(self, at: datetime | None = None) -> SegmentHealthReport:
        return SegmentHealthReport(
            segment_key=self.segment_key,
            state=self.state,
            ewma_success_rate=round(self.ewma, 6),
            baseline_success_rate=round(self.baseline, 6),
            cusum=round(self.cusum, 6),
            observations=self.n,
            evaluated_at=at or self.last_at or _epoch(),
        )


def _epoch() -> datetime:
    from antar import clock

    return clock.now()


class ChangepointDetector:
    """Tracks every segment and answers "how healthy was this segment at time t?".

    Two access patterns, deliberately separated:

      * `observe()` folds in attempts as they arrive - the online path.
      * `health_at()` answers a historical question, which is what the batch
        evaluation needs. A detector that could only report *now* would force the
        evaluation to replay the world for every event.
    """

    def __init__(
        self,
        *,
        ewma_alpha: float = 0.20,
        cusum_k: float = 0.5,
        cusum_h: float = 4.0,
        min_observations: int = 20,
        degrading_threshold: float = 2.0,
        degraded_threshold: float = 4.0,
    ) -> None:
        self.params = {
            "ewma_alpha": ewma_alpha,
            "cusum_k": cusum_k,
            "cusum_h": cusum_h,
            "min_observations": min_observations,
            "degrading_threshold": degrading_threshold,
            "degraded_threshold": degraded_threshold,
        }
        self.trackers: dict[str, SegmentTracker] = {}
        self.history: dict[str, list[tuple[datetime, SegmentHealth]]] = {}

    @classmethod
    def from_config(cls, config) -> ChangepointDetector:
        section = config.section("detect.changepoint")
        return cls(
            ewma_alpha=float(section.get("ewma_alpha")),
            cusum_k=float(section.get("cusum_k")),
            cusum_h=float(section.get("cusum_h")),
            min_observations=int(section.get("min_observations")),
            degrading_threshold=float(section.get("degrading_threshold")),
            degraded_threshold=float(section.get("degraded_threshold")),
        )

    def tracker(self, segment_key: str) -> SegmentTracker:
        tracker = self.trackers.get(segment_key)
        if tracker is None:
            tracker = SegmentTracker(segment_key=segment_key, **self.params)
            self.trackers[segment_key] = tracker
            self.history[segment_key] = []
        return tracker

    def observe(self, segment_key: str, *, success: bool, at: datetime) -> SegmentHealth:
        state = self.tracker(segment_key).observe(success, at)
        self.history[segment_key].append((at, state))
        return state

    def ingest(self, observations: Iterable) -> ChangepointDetector:
        """Fold in an ordered stream of `SegmentObservation`-shaped records.

        Ordering matters and is the caller's responsibility - a CUSUM fed
        out-of-order is measuring nothing.
        """
        for observation in observations:
            self.observe(
                observation.segment_key, success=observation.success, at=observation.at
            )
        return self

    def health_at(self, segment_key: str, when: datetime) -> SegmentHealth:
        """The state the detector was in at `when`, using only data up to then.

        Binary search over the recorded history rather than a replay. Returns
        HEALTHY for an instant before the segment's first observation, which is the
        honest answer: we had no evidence of trouble.
        """
        history = self.history.get(segment_key)
        if not history:
            return SegmentHealth.HEALTHY

        low, high = 0, len(history)
        while low < high:
            mid = (low + high) // 2
            if history[mid][0] <= when:
                low = mid + 1
            else:
                high = mid
        return SegmentHealth.HEALTHY if low == 0 else history[low - 1][1]

    def report(self, segment_key: str) -> SegmentHealthReport:
        return self.tracker(segment_key).report()

    def reports(self) -> list[SegmentHealthReport]:
        return [t.report() for t in sorted(self.trackers.values(), key=lambda x: x.segment_key)]

    def degraded_segments(self) -> list[str]:
        return sorted(
            key
            for key, tracker in self.trackers.items()
            if tracker.state in (SegmentHealth.DEGRADED, SegmentHealth.DEGRADING)
        )
