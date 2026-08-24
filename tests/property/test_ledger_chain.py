"""The ledger chain, under hypothesis.

PLAN.md 9.1 names the ledger chain as one of the four things property tests should
cover, alongside the constraint compiler, the gate, and the mandate FSM — *"These catch
the bugs that matter."*

The example-based tests in `tests/unit/test_ledger.py` tamper in the ways I thought of.
These generate the tampering. The distinction matters because the interesting property
is universal — *no single-field edit anywhere in a chain of any length goes unnoticed* —
and a handful of hand-written examples cannot express it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from antar import clock
from antar.audit.ledger import (
    GENESIS,
    Ledger,
    canonical_json,
    payload_hash,
    verify_entries,
)
from antar.signals.schemas import LedgerKind

AT = datetime.fromisoformat("2026-08-25T11:04:00+05:30")

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Payloads are JSON objects of the shapes Antar actually writes: string ids, integer
# paise, booleans, nested dicts. Deliberately not arbitrary JSON - a float payload would
# exercise round-tripping rather than the chain.
scalars = st.one_of(
    st.text(min_size=0, max_size=24),
    st.integers(min_value=-10**9, max_value=10**9),
    st.booleans(),
    st.none(),
)
payloads = st.dictionaries(
    keys=st.text(min_size=1, max_size=12),
    values=st.one_of(scalars, st.lists(scalars, max_size=4)),
    min_size=1,
    max_size=6,
)
kinds = st.sampled_from(list(LedgerKind))


def build(entries: list[tuple[LedgerKind, dict]]) -> Ledger:
    ledger = Ledger()
    for offset, (kind, payload) in enumerate(entries):
        ledger.append(kind, payload, written_at=AT + timedelta(seconds=offset))
    return ledger


# ------------------------------------------------------- the chain holds


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=0, max_size=12))
def test_any_untouched_chain_verifies(entries):
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        result = ledger.verify_chain()
        assert result.ok, result.breaks
        assert result.entries_checked == len(entries)


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=1, max_size=10))
def test_sequence_numbers_are_contiguous_from_one(entries):
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        assert [e.seq for e in ledger.entries()] == list(range(1, len(entries) + 1))
        assert ledger.entries()[0].prev_hash == GENESIS


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=1, max_size=10))
def test_appending_never_changes_an_existing_entry(entries):
    """Append-only as a property, not as a trigger.

    The SQLite triggers make an UPDATE fail. They say nothing about whether a *legal*
    append disturbs what came before, which is the assumption every later verification
    rests on.
    """
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        before = [(e.seq, e.prev_hash, e.payload_hash, e.kind) for e in ledger.entries()]
        ledger.append(LedgerKind.ALERT, {"appended": "later"})
        after = [(e.seq, e.prev_hash, e.payload_hash, e.kind) for e in ledger.entries()]
        assert after[: len(before)] == before


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=1, max_size=8))
def test_the_head_changes_on_every_append(entries):
    """Otherwise a published head would not distinguish two different ledgers."""
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = Ledger()
        heads = [ledger.head()]
        for offset, (kind, payload) in enumerate(entries):
            ledger.append(kind, payload, written_at=AT + timedelta(seconds=offset))
            heads.append(ledger.head())
        assert len(set(heads)) == len(heads), "two different chain states share a head"


# ---------------------------------------------------- the chain detects


@SETTINGS
@given(
    st.lists(st.tuples(kinds, payloads), min_size=1, max_size=8),
    st.data(),
)
def test_any_single_payload_edit_is_detected(entries, data):
    """The universal claim: *any* payload edit, at *any* position, in a chain of *any*
    length up to eight."""
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        target = data.draw(st.integers(min_value=1, max_value=len(entries)))
        replacement = data.draw(payloads)
        original = ledger.entries()[target - 1].payload
        if replacement == original:
            return  # not an edit

        ledger._disable_append_only_guards()
        ledger._conn.execute(
            "UPDATE ledger SET payload = ? WHERE seq = ?",
            (canonical_json(replacement), target),
        )
        ledger._conn.commit()

        assert not ledger.verify_chain().ok


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=2, max_size=8), st.data())
def test_a_forger_who_recomputes_the_payload_hash_is_still_caught(entries, data):
    """The attacker who has read `ledger.py`.

    Recomputing `payload_hash` makes the edited entry internally consistent. The next
    entry's `prev_hash` covers the whole of the edited one, so the break simply moves
    forward by one - which is why the chain link is not over the payload alone
    (ADR-0024).
    """
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        # Not the last entry: a forged tail has nothing after it to link back.
        target = data.draw(st.integers(min_value=1, max_value=len(entries) - 1))
        replacement = data.draw(payloads)
        if replacement == ledger.entries()[target - 1].payload:
            return

        ledger._disable_append_only_guards()
        ledger._conn.execute(
            "UPDATE ledger SET payload = ?, payload_hash = ? WHERE seq = ?",
            (canonical_json(replacement), payload_hash(replacement), target),
        )
        ledger._conn.commit()

        result = ledger.verify_chain()
        assert not result.ok
        assert any(b.kind == "CHAIN_BROKEN" for b in result.breaks)


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=2, max_size=8), st.data())
def test_deleting_any_entry_but_the_last_is_detected(entries, data):
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        target = data.draw(st.integers(min_value=1, max_value=len(entries) - 1))

        ledger._disable_append_only_guards()
        ledger._conn.execute("DELETE FROM ledger WHERE seq = ?", (target,))
        ledger._conn.commit()

        result = ledger.verify_chain()
        assert not result.ok
        assert any(b.kind == "SEQUENCE_GAP" for b in result.breaks)


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=2, max_size=8), st.data())
def test_reordering_two_entries_is_detected(entries, data):
    """Two entries with their sequence numbers swapped. Every payload is authentic and
    every payload hash is correct; only the order is a lie."""
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        first = data.draw(st.integers(min_value=1, max_value=len(entries) - 1))
        rows = {e.seq: e for e in ledger.entries()}
        if rows[first].payload == rows[first + 1].payload:
            return

        ledger._disable_append_only_guards()
        ledger._conn.execute("UPDATE ledger SET seq = -1 WHERE seq = ?", (first,))
        ledger._conn.execute("UPDATE ledger SET seq = ? WHERE seq = ?", (first, first + 1))
        ledger._conn.execute("UPDATE ledger SET seq = ? WHERE seq = -1", (first + 1,))
        ledger._conn.commit()

        assert not ledger.verify_chain().ok


# ------------------------------------------------------- canonical form


@SETTINGS
@given(payloads)
def test_hashing_is_independent_of_key_order(payload):
    shuffled = dict(reversed(list(payload.items())))
    assert payload_hash(payload) == payload_hash(shuffled)


@SETTINGS
@given(payloads)
def test_canonical_json_round_trips(payload):
    """Whatever we hash must be what we can read back, or a verified chain would still
    be unreadable."""
    assert json.loads(canonical_json(payload)) == payload


@SETTINGS
@given(payloads, payloads)
def test_different_payloads_hash_differently(left, right):
    if left == right:
        return
    assert payload_hash(left) != payload_hash(right)


@SETTINGS
@given(st.lists(st.tuples(kinds, payloads), min_size=0, max_size=6))
def test_verify_entries_agrees_with_the_ledgers_own_verification(entries):
    """The pure function and the I/O wrapper must not drift apart: the console calls
    one and the batch runner calls the other."""
    with clock.use_clock(clock.FrozenClock(AT)):
        ledger = build(entries)
        assert verify_entries(ledger.entries()).as_dict() == ledger.verify_chain().as_dict()
