"""Wiring the detection layer together, once, so nobody assembles it differently twice.

Building the L2 stack correctly involves an ordering that is easy to get wrong:
segment observations must be folded in chronologically before any diagnosis asks for
a historical health state, the classifier must be fitted only on treatment-arm events,
and the mandate FSM must see lifecycle events in payload order. This module owns that
ordering; the batch runner, the console, and the tests all call it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from antar.config import Config, get_config
from antar.detect.changepoint import ChangepointDetector
from antar.detect.classifier import (
    CLASS_ORDER,
    DetectionContext,
    FailureClassifier,
)
from antar.detect.mandate_fsm import MandateRegistry
from antar.detect.root_cause import RootCauseAnalyser, source_mix, unknown_rate
from antar.signals.downtime import DowntimeRegistry
from antar.signals.schemas import AtRiskEvent, Diagnosis, SegmentHealth


@dataclass
class DetectionResult:
    diagnoses: list[Diagnosis]
    by_event: dict[str, Diagnosis] = field(default_factory=dict)
    unknown_rate: float = 0.0
    source_mix: dict[str, int] = field(default_factory=dict)
    classifier_version: str = "none"

    def get(self, event_id: str) -> Diagnosis:
        return self.by_event[event_id]


def build_detector(
    batch: Any,
    *,
    config: Config | None = None,
    train_event_ids: Sequence[str] | None = None,
    fit_classifier: bool = True,
) -> RootCauseAnalyser:
    """Assemble the L2 stack against a simulated batch.

    `train_event_ids` restricts classifier fitting to a permitted set - in practice
    the treatment arm, so that no control event is ever used to fit anything
    (docs/EVALUATION.md section 3.4). Passing `None` trains on everything, which is
    only appropriate in a unit test.
    """
    cfg = config or get_config()

    # Component retention (docs/EVALUATION.md 12.2). Both of these were switched off
    # by the post-M6 ablation, not by an opinion: see `detect.enable_*` in
    # config/default.yaml for the measured deltas.
    #
    # The flags live in config rather than being read from `antar/eval/retention.py`
    # because `antar.detect` may not import `antar.eval` - the evaluation harness is
    # allowed to know ground truth, so importing it upward would be a leak by a longer
    # route, and `tests/statistical/test_no_leakage.py` enforces that. Config is the
    # seam; `scripts/run_evaluation.py` is what writes the verdict into it.
    use_changepoint = bool(cfg.get("detect.enable_changepoint_detector", True))
    use_downtime = bool(cfg.get("detect.enable_downtime_crosscheck", True))

    changepoint = (
        ChangepointDetector.from_config(cfg).ingest(batch.observations)
        if use_changepoint
        else None
    )

    # Driven by the observable subscription webhook stream only. An earlier version
    # derived it from `batch.true_failure_class`, which handed L2 the answer key and
    # inflated MANDATE_REVOKED recall to 1.00. POSTMORTEM D10.
    mandates = MandateRegistry().apply_stream(batch.lifecycle_events)

    # A disconnected component sees an empty registry rather than being special-cased
    # inside the analyser: the analyser's logic stays identical whether or not the
    # feed is switched on, so turning it back on cannot resurrect a different code path.
    declared = batch.downtime if use_downtime else DowntimeRegistry()

    classifier: FailureClassifier | None = None
    if fit_classifier:
        classifier = FailureClassifier.from_config(cfg, seed=batch.seed)
        allowed = set(train_event_ids) if train_event_ids is not None else None
        training = [
            event
            for event in batch.events
            if (allowed is None or event.event_id in allowed)
            and batch.true_failure_class.get(event.event_id) in set(CLASS_ORDER)
        ]
        if len(training) >= 50:
            contexts = [
                DetectionContext(
                    segment_health=(
                        changepoint.health_at(e.segment_key, e.occurred_at)
                        if changepoint is not None
                        else SegmentHealth.HEALTHY
                    ),
                    downtime=(
                        declared.overlap_for(
                            e.occurred_at,
                            e.method,
                            e.issuer,
                            tolerance_minutes=int(
                                cfg.get("detect.downtime_overlap_tolerance_minutes")
                            ),
                        )
                        if use_downtime
                        else None
                    ),
                )
                for e in training
            ]
            labels = [batch.true_failure_class[e.event_id] for e in training]
            classifier.fit(
                training, labels, contexts, ceilings=cfg.get("policy.afa_free_ceiling_paise")
            )
        else:
            classifier = None

    return RootCauseAnalyser(
        downtime=declared,
        mandates=mandates,
        classifier=classifier,
        changepoint=changepoint,
        downtime_tolerance_minutes=int(cfg.get("detect.downtime_overlap_tolerance_minutes")),
        afa_ceilings=cfg.get("policy.afa_free_ceiling_paise"),
    )


def run_detection(
    batch: Any,
    events: Sequence[AtRiskEvent] | None = None,
    *,
    config: Config | None = None,
    train_event_ids: Sequence[str] | None = None,
) -> DetectionResult:
    analyser = build_detector(batch, config=config, train_event_ids=train_event_ids)
    target = list(events if events is not None else batch.events)
    diagnoses = analyser.diagnose_all(target)
    return DetectionResult(
        diagnoses=diagnoses,
        by_event={d.event_id: d for d in diagnoses},
        unknown_rate=unknown_rate(diagnoses),
        source_mix=source_mix(diagnoses),
        classifier_version=(
            analyser.classifier.version if analyser.classifier else "none"
        ),
    )
