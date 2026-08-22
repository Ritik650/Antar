"""Measure the detection layer. PLAN.md M3 acceptance.

Reports, for a held-out slice:

  * per-class precision and recall
  * the cost of a **wrong action** in rupees for each class, not just a confusion matrix
  * changepoint detection delay and false-alarm rate against injected downtime
  * the `UNKNOWN` rate, reported honestly
  * which evidence source actually decided each case

    python -m scripts.detection_report --scenario base
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

from antar.config import artifacts_dir, get_config
from antar.detect.classifier import CLASS_ORDER
from antar.detect.pipeline import build_detector
from antar.detect.root_cause import action_cost
from antar.eval.holdout import Holdout
from antar.signals.schemas import FailureClass, SegmentHealth
from antar.simulator.generator import generate


def evaluate_detection(batch, config) -> dict:
    holdout = Holdout(
        control_share=float(config.get("eval.control_share")),
        salt=str(config.get("eval.experiment_salt")),
    )
    # Fit only on the treatment arm; score only on the control arm. Neither the
    # classifier nor its thresholds ever see the events they are graded on.
    train_ids = [e.event_id for e in holdout.training_filter(batch.events)]
    _, control = holdout.split(batch.events)

    analyser = build_detector(batch, config=config, train_event_ids=train_ids)
    diagnoses = analyser.diagnose_all(control)

    truth = [batch.true_failure_class.get(d.event_id, FailureClass.UNKNOWN) for d in diagnoses]
    predicted = [d.failure_class for d in diagnoses]
    amounts = {e.event_id: e.amount_paise for e in control}

    per_class = {}
    for cls in CLASS_ORDER:
        tp = sum(1 for p, t in zip(predicted, truth, strict=True) if p == cls and t == cls)
        fp = sum(1 for p, t in zip(predicted, truth, strict=True) if p == cls and t != cls)
        fn = sum(1 for p, t in zip(predicted, truth, strict=True) if p != cls and t == cls)
        # Action-denominated: what the merchant actually does when we say `cls`,
        # priced against what the truth demanded. POSTMORTEM D11.
        cost = sum(
            amounts[d.event_id] * action_cost(d.recommended_class, t)
            for d, p, t in zip(diagnoses, predicted, truth, strict=True)
            if p == cls and t != cls
        )
        per_class[cls.value] = {
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "support": tp + fn,
            "predicted": tp + fp,
            "wrong_action_cost_rupees": round(cost / 100, 2),
        }

    from collections import Counter

    from antar.simulator.failure_emission import UNMAPPED_REASONS

    # What happens to codes the taxonomy table has never seen. Reported separately
    # because the headline UNKNOWN rate is otherwise a property of the generator
    # rather than evidence of coverage - see the caveat below.
    unmapped = [
        (d, t)
        for d, t, e in zip(diagnoses, truth, control, strict=True)
        if e.error_reason in UNMAPPED_REASONS
    ]
    unmapped_correct = sum(1 for d, t in unmapped if d.failure_class == t)
    unmapped_abstained = sum(1 for d, _ in unmapped if d.failure_class is FailureClass.UNKNOWN)

    return {
        "scenario": batch.scenario.name,
        "seed": batch.seed,
        "evaluated_on": "control arm (never used for fitting)",
        "n_events": len(diagnoses),
        "unknown_rate_caveat": (
            "The UNKNOWN rate is a property of the generator as much as of the "
            "detector. The simulator draws error codes from the documented Razorpay "
            "taxonomy plus a deliberately unmapped minority "
            f"({len(unmapped)} of {len(diagnoses)} events here); a real error stream "
            "contains far more vendor variants and post-dated codes. Read this figure "
            "as a lower bound on the UNKNOWN rate Antar would see in production, not "
            "as evidence that the mapping is complete."
        ),
        "unmapped_codes": {
            "events": len(unmapped),
            "share": round(len(unmapped) / max(len(diagnoses), 1), 4),
            "abstained_to_unknown": unmapped_abstained,
            "resolved_correctly": unmapped_correct,
            "resolved_incorrectly": len(unmapped) - unmapped_correct - unmapped_abstained,
        },
        "unknown_rate": round(
            sum(1 for p in predicted if p is FailureClass.UNKNOWN) / max(len(predicted), 1), 4
        ),
        "accuracy_when_resolved": round(
            sum(
                1
                for p, t in zip(predicted, truth, strict=True)
                if p is not FailureClass.UNKNOWN and p == t
            )
            / max(sum(1 for p in predicted if p is not FailureClass.UNKNOWN), 1),
            4,
        ),
        "per_class": per_class,
        "source_mix": dict(sorted(Counter(d.source for d in diagnoses).items())),
        "total_wrong_action_cost_rupees": round(
            sum(v["wrong_action_cost_rupees"] for v in per_class.values()), 2
        ),
    }


def evaluate_ablation(batch, config) -> dict:
    """Does each evidence source earn its place?

    PLAN.md's "AI judgment" criterion asks where you chose *not* to use a model, and
    the honest version of that question is whether the components you did use are
    carrying weight. So each is switched off in turn and the rupee-denominated
    misclassification cost is re-measured.

    The headline number to watch is `ISSUER_DOWN` precision against total cost. The
    detector deliberately over-calls outages: the cost matrix prices a missed outage
    (waiting was right, we terminated) at 1.0x the cycle and a false one (we waited
    when we could have acted) at 0.25-0.40x. A precision of 0.55 at recall 0.98 is
    not a defect if the cheaper error is the one being made - but that is a claim
    about a cost function, so it is measured here rather than asserted.
    """
    from antar.detect.changepoint import ChangepointDetector
    from antar.detect.mandate_fsm import MandateRegistry
    from antar.detect.root_cause import RootCauseAnalyser
    from antar.signals.downtime import DowntimeRegistry

    holdout = Holdout(
        control_share=float(config.get("eval.control_share")),
        salt=str(config.get("eval.experiment_salt")),
    )
    train_ids = [e.event_id for e in holdout.training_filter(batch.events)]
    _, control = holdout.split(batch.events)
    full = build_detector(batch, config=config, train_event_ids=train_ids)
    amounts = {e.event_id: e.amount_paise for e in control}

    changepoint = ChangepointDetector.from_config(config).ingest(batch.observations)
    mandates = MandateRegistry().apply_stream(batch.lifecycle_events)
    ceilings = config.get("policy.afa_free_ceiling_paise")
    tolerance = int(config.get("detect.downtime_overlap_tolerance_minutes"))

    variants = {
        "table_only": RootCauseAnalyser(
            downtime=DowntimeRegistry(), mandates=MandateRegistry(), classifier=None,
            changepoint=None, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "plus_mandate_fsm": RootCauseAnalyser(
            downtime=DowntimeRegistry(), mandates=mandates, classifier=None,
            changepoint=None, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "plus_downtime_feed": RootCauseAnalyser(
            downtime=batch.downtime, mandates=mandates, classifier=None,
            changepoint=None, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "plus_changepoint": RootCauseAnalyser(
            downtime=batch.downtime, mandates=mandates, classifier=None,
            changepoint=changepoint, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "plus_classifier_full": full,
        # Leave-one-out from the finished system. This is the question that actually
        # matters - "does this component earn its place in the system as built?" -
        # and it gives a different answer from the incremental sweep above.
        #
        # Adding the downtime feed to a table-only detector looks worthless, because
        # an unresolved event already defaults to WAIT and WAIT is the right action
        # during an outage. Its value only appears once a classifier is resolving
        # those events into actions: then the feed is what stops the classifier
        # confidently choosing CONTACT in the middle of an outage.
        "full_minus_downtime": RootCauseAnalyser(
            downtime=DowntimeRegistry(), mandates=mandates, classifier=full.classifier,
            changepoint=changepoint, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "full_minus_changepoint": RootCauseAnalyser(
            downtime=batch.downtime, mandates=mandates, classifier=full.classifier,
            changepoint=None, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
        "full_minus_mandate_fsm": RootCauseAnalyser(
            downtime=batch.downtime, mandates=MandateRegistry(), classifier=full.classifier,
            changepoint=changepoint, downtime_tolerance_minutes=tolerance, afa_ceilings=ceilings,
        ),
    }

    out = {}
    for name, analyser in variants.items():
        diagnoses = analyser.diagnose_all(control)
        truth = [batch.true_failure_class.get(d.event_id, FailureClass.UNKNOWN) for d in diagnoses]
        predicted = [d.failure_class for d in diagnoses]
        cost = sum(
            amounts[d.event_id] * action_cost(d.recommended_class, t)
            for d, p, t in zip(diagnoses, predicted, truth, strict=True)
        )
        resolved = [p for p in predicted if p is not FailureClass.UNKNOWN]
        issuer_tp = sum(
            1
            for p, t in zip(predicted, truth, strict=True)
            if p is FailureClass.ISSUER_DOWN and t is FailureClass.ISSUER_DOWN
        )
        issuer_pred = sum(1 for p in predicted if p is FailureClass.ISSUER_DOWN)
        issuer_true = sum(1 for t in truth if t is FailureClass.ISSUER_DOWN)
        out[name] = {
            "unknown_rate": round(1 - len(resolved) / max(len(predicted), 1), 4),
            "accuracy_when_resolved": round(
                sum(
                    1
                    for p, t in zip(predicted, truth, strict=True)
                    if p is not FailureClass.UNKNOWN and p == t
                )
                / max(len(resolved), 1),
                4,
            ),
            "issuer_down_precision": round(issuer_tp / issuer_pred, 4) if issuer_pred else None,
            "issuer_down_recall": round(issuer_tp / issuer_true, 4) if issuer_true else None,
            "total_misclassification_cost_rupees": round(cost / 100, 2),
        }
    return out


def evaluate_changepoint(batch, config) -> dict:
    """Detection delay and false-alarm rate against the injected downtime windows.

    Delay is measured from the start of a window to the first observation in that
    segment at which the detector reports DEGRADING or worse. A window with no
    attempts inside it is not counted: the detector cannot see what it is not shown,
    and counting it as a miss would flatter or damn the detector arbitrarily.
    """
    from antar.detect.changepoint import ChangepointDetector

    detector = ChangepointDetector.from_config(config).ingest(batch.observations)

    delays: list[float] = []
    missed = 0
    windows = 0
    for window in batch.all_downtime:
        if window.end is None:
            continue
        segment = f"{window.issuer}:{window.method.value}"
        history = detector.history.get(segment, [])
        inside = [(at, state) for at, state in history if window.begin <= at <= window.end]
        if not inside:
            continue
        windows += 1
        flagged = next(
            (
                at
                for at, state in inside
                if state in (SegmentHealth.DEGRADING, SegmentHealth.DEGRADED)
            ),
            None,
        )
        if flagged is None:
            missed += 1
        else:
            delays.append((flagged - window.begin) / timedelta(hours=1))

    # False alarms: observations flagged degraded with no window covering them.
    flagged_clean = 0
    clean_total = 0
    for segment, history in detector.history.items():
        issuer, method = segment.split(":")
        for at, state in history:
            covered = any(
                w.covers(at) and w.issuer == issuer and w.method.value == method
                for w in batch.all_downtime
            )
            if covered:
                continue
            clean_total += 1
            if state in (SegmentHealth.DEGRADING, SegmentHealth.DEGRADED):
                flagged_clean += 1

    delays_sorted = sorted(delays)
    return {
        "windows_with_attempts": windows,
        "windows_detected": windows - missed,
        "windows_missed": missed,
        "detection_rate": round((windows - missed) / windows, 4) if windows else None,
        "median_detection_delay_hours": (
            round(delays_sorted[len(delays_sorted) // 2], 3) if delays_sorted else None
        ),
        "mean_detection_delay_hours": (
            round(sum(delays) / len(delays), 3) if delays else None
        ),
        "false_alarm_rate": round(flagged_clean / clean_total, 4) if clean_total else None,
        "clean_observations": clean_total,
    }


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    args = parser.parse_args()

    print(f"generating {args.scenario} (seed {args.seed})...", flush=True)
    batch = generate(args.scenario, seed=args.seed)

    report = {
        "detection": evaluate_detection(batch, config),
        "ablation": evaluate_ablation(batch, config),
        "changepoint": evaluate_changepoint(batch, config),
        "taxonomy_coverage": __import__(
            "antar.detect.taxonomy", fromlist=["coverage_report"]
        ).coverage_report(),
    }

    out = artifacts_dir() / f"detection_{args.scenario}.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    d = report["detection"]
    print(f"\nDetection on {d['n_events']} control-arm events ({d['evaluated_on']})")
    u = d["unmapped_codes"]
    print(
        f"  UNKNOWN rate            : {d['unknown_rate']:.1%}  "
        f"(generator artifact - see unknown_rate_caveat)"
    )
    print(
        f"  unmapped codes          : {u['events']} events "
        f"({u['share']:.1%}); {u['abstained_to_unknown']} abstained, "
        f"{u['resolved_correctly']} resolved correctly, "
        f"{u['resolved_incorrectly']} wrong"
    )
    print(f"  accuracy when resolved  : {d['accuracy_when_resolved']:.1%}")
    print(f"  wrong-action cost       : Rs {d['total_wrong_action_cost_rupees']:,.0f}")
    print(f"  decided by              : {d['source_mix']}")
    print(f"\n  {'class':22s} {'prec':>6s} {'rec':>6s} {'n':>6s}  cost (Rs)   ")
    for name, row in d["per_class"].items():
        prec = f"{row['precision']:.3f}" if row["precision"] is not None else "  -  "
        rec = f"{row['recall']:.3f}" if row["recall"] is not None else "  -  "
        print(
            f"  {name:22s} {prec:>6s} {rec:>6s} {row['support']:>6d}  "
            f"{row['wrong_action_cost_rupees']:>12,.0f}"
        )

    print(f"\n  {'ablation':22s} {'unk':>6s} {'acc':>6s} {'ID-P':>6s} {'ID-R':>6s}  cost (Rs)")
    for name, row in report["ablation"].items():
        fmt = lambda v: f"{v:.3f}" if v is not None else "  -  "  # noqa: E731
        print(
            f"  {name:22s} {row['unknown_rate']:>6.3f} "
            f"{row['accuracy_when_resolved']:>6.3f} "
            f"{fmt(row['issuer_down_precision']):>6s} {fmt(row['issuer_down_recall']):>6s}  "
            f"{row['total_misclassification_cost_rupees']:>12,.0f}"
        )

    c = report["changepoint"]
    print(
        f"\nChangepoint: detected {c['windows_detected']}/{c['windows_with_attempts']} windows, "
        f"median delay {c['median_detection_delay_hours']}h, "
        f"false-alarm rate {c['false_alarm_rate']}"
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
