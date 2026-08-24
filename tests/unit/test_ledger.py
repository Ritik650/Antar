"""The audit ledger, and the tamper test PLAN.md M8 requires.

> `ledger.py`: append-only, hash-chained (`prev_hash` + `payload_hash`). A
> `verify_chain()` routine that detects tampering. Tamper-detection test included.

Each tamper test does *real* damage with the append-only triggers removed. A tamper
test that only simulates tampering proves that the simulation works.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

import pytest

from antar import clock
from antar.audit.ledger import (
    GENESIS,
    CanonicalisationError,
    Ledger,
    canonical_json,
    entry_hash,
    payload_hash,
    verify_entries,
)
from antar.signals.schemas import LedgerKind

AT = datetime.fromisoformat("2026-08-24T11:04:00+05:30")


@pytest.fixture
def frozen():
    with clock.use_clock(clock.FrozenClock(AT)) as c:
        yield c


@pytest.fixture
def ledger(frozen) -> Ledger:
    led = Ledger()
    led.append(LedgerKind.EVENT, {"event_id": "evt_1", "amount_paise": 49900})
    led.append(LedgerKind.DIAGNOSIS, {"event_id": "evt_1", "failure_class": "SOFT_DECLINE"})
    led.append(LedgerKind.DECISION, {"event_id": "evt_1", "decision_id": "dec_1"})
    led.append(LedgerKind.ACTION, {"decision_id": "dec_1", "action_id": "act_1"})
    led.append(LedgerKind.OUTCOME, {"action_id": "act_1", "recovered": True})
    return led


# --------------------------------------------------------------- the chain


def test_a_fresh_chain_verifies(ledger):
    result = ledger.verify_chain()
    assert result.ok
    assert result.entries_checked == 5
    assert result.breaks == ()


def test_the_first_entry_links_to_genesis(ledger):
    first = ledger.entries()[0]
    assert first.seq == 1
    assert first.prev_hash == GENESIS


def test_each_entry_links_to_the_whole_of_the_one_before(ledger):
    entries = ledger.entries()
    for previous, current in itertools.pairwise(entries):
        assert current.prev_hash == entry_hash(
            seq=previous.seq,
            prev_hash=previous.prev_hash,
            payload_hash_=previous.payload_hash,
            kind=previous.kind,
            written_at=previous.written_at,
        )


def test_the_head_is_the_hash_of_the_last_entry(ledger):
    last = ledger.entries()[-1]
    assert ledger.head() == entry_hash(
        seq=last.seq,
        prev_hash=last.prev_hash,
        payload_hash_=last.payload_hash,
        kind=last.kind,
        written_at=last.written_at,
    )


def test_an_empty_ledger_heads_at_genesis(frozen):
    assert Ledger().head() == GENESIS
    assert Ledger().verify_chain().ok


# ------------------------------------------------------------- tampering


def test_editing_a_payload_is_detected(ledger):
    ledger._disable_append_only_guards()
    ledger._conn.execute(
        "UPDATE ledger SET payload = ? WHERE seq = 1",
        (json.dumps({"event_id": "evt_1", "amount_paise": 4_990_000}),),
    )
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert not result.ok
    assert result.first_break.seq == 1
    assert result.first_break.kind == "PAYLOAD_MODIFIED"


def test_editing_a_payload_and_its_hash_still_breaks_the_chain(ledger):
    """The interesting case: an attacker who knows how the hash is computed.

    Recomputing `payload_hash` makes the entry internally consistent, and the *next*
    entry's `prev_hash` immediately gives it away.
    """
    ledger._disable_append_only_guards()
    forged = {"event_id": "evt_1", "amount_paise": 4_990_000}
    ledger._conn.execute(
        "UPDATE ledger SET payload = ?, payload_hash = ? WHERE seq = 1",
        (canonical_json(forged), payload_hash(forged)),
    )
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert not result.ok
    assert [b.kind for b in result.breaks] == ["CHAIN_BROKEN"]
    assert result.first_break.seq == 2, "the break surfaces at the entry that links back"


def test_changing_only_the_kind_is_detected(ledger):
    """Why the link covers the metadata and not just the payload.

    A chain over payload hashes alone would accept this: the payload is untouched, so
    its hash still agrees. Relabelling an ACTION as an ALERT is exactly how a send
    would be hidden.
    """
    ledger._disable_append_only_guards()
    ledger._conn.execute("UPDATE ledger SET kind = 'ALERT' WHERE seq = 4")
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert not result.ok
    assert result.first_break.kind == "CHAIN_BROKEN"
    assert result.first_break.seq == 5


def test_moving_a_timestamp_is_detected(ledger):
    """"Why did you contact this customer at 11:04?" is only answerable if 11:04 is
    load-bearing. A timestamp outside the hash is a timestamp anyone can move."""
    ledger._disable_append_only_guards()
    ledger._conn.execute(
        "UPDATE ledger SET written_at = ? WHERE seq = 4",
        ((AT + timedelta(hours=6)).isoformat(),),
    )
    ledger._conn.commit()
    assert not ledger.verify_chain().ok


def test_deleting_an_entry_is_detected(ledger):
    """The half a hash chain does not give you.

    Remove an entry and the survivors are still internally consistent. Only the
    contiguity check turns "unedited" into "complete".
    """
    ledger._disable_append_only_guards()
    ledger._conn.execute("DELETE FROM ledger WHERE seq = 3")
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert not result.ok
    kinds = [b.kind for b in result.breaks]
    assert "SEQUENCE_GAP" in kinds
    assert "CHAIN_BROKEN" in kinds


def test_truncating_the_tail_is_not_detected_by_the_chain_alone(ledger):
    """An honest negative result, and the reason `head()` is published.

    Deleting the *last* entries leaves a shorter chain that verifies perfectly. Nothing
    inside the ledger can detect that, which is why `head()` exists: an auditor who
    recorded the head yesterday compares it today.
    """
    ledger._disable_append_only_guards()
    head_before = ledger.head()
    ledger._conn.execute("DELETE FROM ledger WHERE seq >= 4")
    ledger._conn.commit()

    assert ledger.verify_chain().ok, "the chain itself cannot see a truncation"
    assert ledger.head() != head_before, "but the published head can"


def test_every_break_is_reported_not_just_the_first(ledger):
    ledger._disable_append_only_guards()
    for seq in (1, 3):
        ledger._conn.execute(
            "UPDATE ledger SET payload = ? WHERE seq = ?", (json.dumps({"x": seq}), seq)
        )
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert len([b for b in result.breaks if b.kind == "PAYLOAD_MODIFIED"]) == 2


# ------------------------------------------------------------ append-only


def test_the_storage_engine_refuses_an_update(ledger):
    with pytest.raises(Exception, match="append-only"):
        ledger._conn.execute("UPDATE ledger SET kind = 'ALERT' WHERE seq = 1")


def test_the_storage_engine_refuses_a_delete(ledger):
    with pytest.raises(Exception, match="append-only"):
        ledger._conn.execute("DELETE FROM ledger WHERE seq = 1")


# ------------------------------------------------------- canonical form


def test_key_order_does_not_change_the_hash():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


def test_the_same_payload_hashes_the_same_across_calls():
    payload = {"event_id": "evt_1", "amount_paise": 49900, "at": AT}
    assert payload_hash(payload) == payload_hash(dict(payload))


def test_a_set_is_refused_rather_than_hashed():
    """Two equal sets can iterate in two orders, so a set has no single hash."""
    with pytest.raises(CanonicalisationError, match="iteration order"):
        canonical_json({"channels": {"SMS", "WHATSAPP"}})


def test_an_unknown_type_is_refused_rather_than_stringified():
    class Opaque:
        pass

    with pytest.raises(CanonicalisationError, match="no canonical form"):
        canonical_json({"thing": Opaque()})


def test_a_pydantic_model_is_stored_as_its_json_form(frozen):
    """`record_action` is the `LedgerSink` the gate writes through."""
    from antar.signals.schemas import ActionRecord, Channel, MessageClass

    action = ActionRecord(
        action_id="act_1",
        decision_id="dec_1",
        event_id="evt_1",
        idempotency_key="idem_1",
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        amount_paise=49900,
    )
    led = Ledger()
    led.record_action(action)

    stored = led.entries()[0]
    assert stored.kind is LedgerKind.ACTION
    assert stored.payload["action_id"] == "act_1"
    assert stored.payload["channel"] == "SMS", "enums are stored by value, not by repr"
    assert led.verify_chain().ok


def test_a_refused_action_is_recorded_rather_than_dropped(frozen):
    """An injection that is silently dropped is indistinguishable from one that never
    happened. The refusal is the evidence."""
    from antar.signals.schemas import ActionRecord, Channel, MessageClass

    refused = ActionRecord(
        action_id="act_2",
        decision_id="dec_1",
        event_id="evt_1",
        idempotency_key="idem_2",
        channel=Channel.SMS,
        message_class=MessageClass.TRANSACTIONAL,
        rejected_reason="TRAI-03: promotional content detected (DISCOUNT_OFFER)",
    )
    led = Ledger()
    led.record_action(refused)

    entry = led.entries()[0]
    assert entry.payload["rejected_reason"].startswith("TRAI-03")
    assert entry.payload["executed"] is False


# ------------------------------------------------------------- behaviour


def test_a_non_mapping_payload_is_refused(frozen):
    led = Ledger()
    with pytest.raises(TypeError, match="must be a mapping"):
        led.append(LedgerKind.EVENT, ["not", "a", "mapping"])


def test_entries_can_be_filtered_by_kind(ledger):
    assert [e.kind for e in ledger.entries(kind=LedgerKind.ACTION)] == [LedgerKind.ACTION]


def test_find_matches_on_payload_fields(ledger):
    assert [e.seq for e in ledger.find(event_id="evt_1")] == [1, 2, 3]
    assert ledger.find(event_id="nope") == []


def test_the_written_time_comes_from_the_authoritative_clock(ledger):
    assert all(entry.written_at == AT for entry in ledger.entries())


def test_two_ledgers_built_the_same_way_produce_the_same_head(frozen):
    def build() -> Ledger:
        led = Ledger()
        led.append(LedgerKind.EVENT, {"event_id": "evt_1"})
        led.append(LedgerKind.DECISION, {"decision_id": "dec_1"})
        return led

    assert build().head() == build().head(), (
        "a ledger whose hashes depend on wall time cannot be regression-tested"
    )


def test_verify_entries_is_pure_and_takes_a_list(ledger):
    assert verify_entries(ledger.entries()).ok
    assert verify_entries([]).ok
