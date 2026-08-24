"""Run one batch through every layer and write the audit ledger.

    python -m scripts.run_batch --scenario base
    python tasks.py simulate SCENARIO=base

This is the path the console traces. It produces `artifacts/antar.db`, whose chain is
verified before the script exits, plus `artifacts/batch_<scenario>.json` with the
summary and the ledger head.

**Nothing here can send.** The gate is constructed with `gate.dry_run` from config,
which defaults to true, and no Razorpay client is passed to it. Every recovery rate
below is simulated (N6).
"""

from __future__ import annotations

import argparse
import json
import time

from antar.audit.ledger import Ledger
from antar.audit.replay import replay_is_self_consistent
from antar.audit.trace import TraceIndex
from antar.config import artifacts_dir, get_config
from antar.pipeline import NoModel, run_pipeline


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="base")
    parser.add_argument("--seed", type=int, default=int(config.get("run.seed")))
    parser.add_argument("--customers", type=int, default=0, help="0 = config default")
    parser.add_argument("--max-events", type=int, default=0, help="0 = all")
    parser.add_argument("--policy", default="antar", help="accepted for symmetry; only antar writes a ledger")
    parser.add_argument(
        "--db",
        default="",
        help="ledger path; default artifacts/batch_<scenario>.db. ':memory:' to discard.",
    )
    parser.add_argument("--trace", default="", help="print the full trace for one event id")
    args = parser.parse_args()

    if args.customers:
        config = config.with_overrides(
            {
                "simulator.n_customers": args.customers,
                "simulator.checkout_abandon_events": args.customers // 3,
            }
        )

    path = args.db or str(artifacts_dir() / f"batch_{args.scenario}.db")
    if path not in (":memory:",):
        from pathlib import Path

        # A ledger is append-only, so a rerun must start a new one rather than
        # appending a second batch onto the first and reporting the total.
        Path(path).unlink(missing_ok=True)

    print(f"running {args.scenario} (seed {args.seed}) -> {path}", flush=True)
    started = time.perf_counter()

    ledger = Ledger(path)
    try:
        result, ledger = run_pipeline(
            args.scenario,
            seed=args.seed,
            config=config,
            ledger=ledger,
            max_events=args.max_events or None,
        )
    except NoModel as exc:
        # PLAN.md section 10, row 13. The safe failure for a money system is do nothing.
        print(f"\nREFUSING TO ACT: {exc}")
        print("No contacts were made and no ledger actions were written. This is correct.")
        return 2

    elapsed = time.perf_counter() - started
    summary = result.as_dict()

    print(f"\n  {result.events} events in {elapsed:.0f}s")
    print(f"  {result.decided} decided, {result.contacted} contacted, "
          f"{result.abstained} abstained, {result.control} in holdout, "
          f"{result.refused} refused by the gate")
    print(f"  simulated: {result.recovered} recovered, {result.optouts} opt-outs")
    print(f"  model {result.model_version}, policy {result.policy_version}")
    for note in result.notes:
        print(f"  note: {note}")

    verification = ledger.verify_chain()
    print(f"\n  ledger: {len(ledger)} entries, head {ledger.head()[:16]}...")
    print(f"  chain verifies: {verification.ok}")
    if not verification.ok:
        for chain_break in verification.breaks[:5]:
            print(f"    {chain_break}")
        return 1

    # The null replay: feed each recorded decision back as its own answer. Must be
    # perfect by construction, which is what makes it a test of the comparator.
    consistency = replay_is_self_consistent(ledger)
    print(f"  self-consistent replay: {consistency.identical}/{consistency.events_replayed}")
    if not consistency.ok:
        print("    the comparator disagreed with the record it was given; that is a bug")
        return 1

    summary["chain_verified"] = verification.ok
    summary["replay_self_consistent"] = consistency.ok
    summary["ledger_path"] = path

    out = artifacts_dir() / f"batch_{args.scenario}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"  wrote {out}")

    index = TraceIndex(ledger)
    event_id = args.trace or _first_contacted(index)
    if event_id:
        print("\n" + "=" * 78)
        print(f'"Why did you contact this customer?" - {event_id}')
        print("=" * 78)
        print(index.trace(event_id).narrate())

    return 0


def _first_contacted(index: TraceIndex) -> str:
    """An event the gate approved, so the printed trace shows the whole path.

    Falls back to the first event of any kind: a batch in which nothing was contacted
    is a legitimate outcome and printing "no trace available" would be less useful than
    printing the abstention and its reason.
    """
    for event_id in index.event_ids():
        if index.trace(event_id).approved:
            return event_id
    ids = index.event_ids()
    return ids[0] if ids else ""


if __name__ == "__main__":
    raise SystemExit(main())
