"""Append-only, hash-chained audit ledger.

PLAN.md M8:

> `ledger.py`: append-only, hash-chained (`prev_hash` + `payload_hash`). A
> `verify_chain()` routine that detects tampering.

Everything Antar does that touches a customer or a rupee lands here: the event that
started it, the diagnosis, the decision and the constraints that bound it, the gate's
verdict, the action, and the outcome. `trace.py` reads it back; `replay.py` replays it.

## What "tamper-evident" actually requires

The obvious implementation chains payload hashes: entry *n* stores
`prev_hash = sha256(payload of n-1)`. It detects an edited payload and **misses
everything else**. Change an entry's `kind` from `ACTION` to `ALERT`, or move its
timestamp by six hours, and every payload hash still agrees with every payload.

So the link is over the **whole entry**. `entry_hash` covers `seq`, `prev_hash`,
`payload_hash`, `kind`, and `written_at`, and entry *n+1* links to *that*. An edit
anywhere in the record breaks the chain at the point of the edit, which is also the
point an auditor needs to be shown.

Deletion is the other half. A chain of hashes detects a *modified* entry but a naive
verifier walking `ORDER BY seq` will happily accept a chain with a hole in it — the
remaining entries are internally consistent. `verify_chain()` therefore checks that
`seq` is contiguous from 1, which is what turns "these records are unedited" into
"these are all the records".

## Append-only is enforced twice

SQLite triggers reject `UPDATE` and `DELETE` on the table, so tampering requires a
deliberate schema change rather than a stray statement. That is a speed bump, not a
guarantee — anyone with the file can drop the trigger — which is exactly why
`verify_chain()` exists and why the tamper test in `tests/unit/test_ledger.py` does its
damage with the triggers disabled. A control that can only be tested by pretending it
works is not a control.

## Canonical form

Hashes are taken over JSON with sorted keys, no insignificant whitespace, and a strict
encoder for datetimes, enums, and `Decimal`. Anything the encoder does not recognise
raises rather than falling back to `str()`: a payload that hashes differently depending
on how Python happened to render an object is not evidence of anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from antar import clock
from antar.signals.schemas import ActionRecord, LedgerEntry, LedgerKind

GENESIS = "0" * 64
"""`prev_hash` of the first entry. A fixed, recognisable value rather than empty
string or NULL, so that "the chain starts here" and "this field was never written"
cannot be confused."""


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


class CanonicalisationError(TypeError):
    """A payload contained something with no single obvious serialisation."""


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Money is integer paise everywhere in Antar; a Decimal here is either a rate
        # or a mistake. Either way its string form is exact and float() is not.
        return str(value)
    if isinstance(value, set | frozenset):
        raise CanonicalisationError(
            f"refusing to hash a {type(value).__name__}: iteration order is not part "
            "of the value, so two equal sets can produce two different hashes. "
            "Convert to a sorted list at the call site, where the ordering is a "
            "decision someone made."
        )
    if isinstance(value, bytes):
        raise CanonicalisationError("refusing to hash raw bytes; encode them explicitly")
    raise CanonicalisationError(
        f"no canonical form for {type(value).__name__}. Add one to _default rather "
        "than letting str() decide, because str() is not stable across versions."
    )


def canonical_json(payload: Any) -> str:
    """The exact bytes that get hashed. Deterministic across processes and runs."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash(payload: Any) -> str:
    return _sha256(canonical_json(payload))


def entry_hash(
    *, seq: int, prev_hash: str, payload_hash_: str, kind: LedgerKind | str, written_at: datetime
) -> str:
    """The link. Covers the metadata as well as the payload — see the module docstring.

    Fields are joined with a delimiter that cannot occur in any of them, so that
    `("ab", "c")` and `("a", "bc")` cannot hash alike.
    """
    kind_value = kind.value if isinstance(kind, LedgerKind) else str(kind)
    joined = "\x1f".join(
        [
            str(int(seq)),
            str(prev_hash),
            str(payload_hash_),
            kind_value,
            written_at.isoformat(),
        ]
    )
    return _sha256(joined)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainBreak:
    seq: int
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"seq {self.seq}: {self.kind} - {self.detail}"


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    entries_checked: int
    breaks: tuple[ChainBreak, ...] = ()

    @property
    def first_break(self) -> ChainBreak | None:
        return self.breaks[0] if self.breaks else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "breaks": [
                {"seq": b.seq, "kind": b.kind, "detail": b.detail} for b in self.breaks
            ],
        }


def verify_entries(entries: Sequence[LedgerEntry]) -> VerificationResult:
    """Check a materialised chain. Pure; `Ledger.verify_chain()` is the I/O wrapper.

    Reports **every** break rather than stopping at the first. An auditor asking "what
    was changed?" is not helped by an answer that only covers the earliest edit.
    """
    breaks: list[ChainBreak] = []
    expected_seq = 1
    expected_prev = GENESIS

    for entry in entries:
        if entry.seq != expected_seq:
            breaks.append(
                ChainBreak(
                    entry.seq,
                    "SEQUENCE_GAP",
                    f"expected seq {expected_seq}, found {entry.seq}. Entries were "
                    "removed, or inserted out of order. A hash chain alone would not "
                    "have noticed.",
                )
            )
            expected_seq = entry.seq

        computed_payload = payload_hash(entry.payload)
        if computed_payload != entry.payload_hash:
            breaks.append(
                ChainBreak(
                    entry.seq,
                    "PAYLOAD_MODIFIED",
                    f"payload hashes to {computed_payload[:12]}... but the entry "
                    f"records {entry.payload_hash[:12]}...",
                )
            )

        if entry.prev_hash != expected_prev:
            breaks.append(
                ChainBreak(
                    entry.seq,
                    "CHAIN_BROKEN",
                    f"prev_hash is {entry.prev_hash[:12]}... but the preceding entry "
                    f"hashes to {expected_prev[:12]}...",
                )
            )

        expected_prev = entry_hash(
            seq=entry.seq,
            prev_hash=entry.prev_hash,
            payload_hash_=entry.payload_hash,
            kind=entry.kind,
            written_at=entry.written_at,
        )
        expected_seq += 1

    return VerificationResult(
        ok=not breaks, entries_checked=len(entries), breaks=tuple(breaks)
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq          INTEGER PRIMARY KEY,
    prev_hash    TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    entry_hash   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    written_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ledger_kind_idx ON ledger (kind);

-- Append-only, enforced by the storage engine. A speed bump rather than a guarantee:
-- anyone who can drop a trigger can drop this one, which is why verify_chain() exists.
CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: entries cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: entries cannot be deleted');
END;
"""

_DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS ledger_no_update;
DROP TRIGGER IF EXISTS ledger_no_delete;
"""


class Ledger:
    """The append-only record. One instance per process; safe across threads.

    Backed by SQLite because `make evaluate` must run from a clean checkout with no
    Docker (ADR-0006). `:memory:` is the test path and behaves identically.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path not in (":memory:", ""):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- writing

    def append(self, kind: LedgerKind, payload: Any, *, written_at: datetime | None = None) -> LedgerEntry:
        """Add one entry. The only way in.

        `written_at` defaults to the authoritative clock, which under `FrozenClock`
        makes the whole chain reproducible — a ledger whose hashes depend on wall time
        cannot be regression-tested.
        """
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        if not isinstance(payload, dict):
            raise TypeError(
                f"a ledger payload must be a mapping, not {type(payload).__name__}. "
                "Entries are read back by field name; a bare list or string has no "
                "field names to read."
            )

        stamped = written_at or clock.now()
        p_hash = payload_hash(payload)

        with self._lock:
            row = self._conn.execute(
                "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = (row["seq"] + 1) if row else 1
            prev = row["entry_hash"] if row else GENESIS

            e_hash = entry_hash(
                seq=seq,
                prev_hash=prev,
                payload_hash_=p_hash,
                kind=kind,
                written_at=stamped,
            )
            self._conn.execute(
                "INSERT INTO ledger (seq, prev_hash, payload_hash, entry_hash, kind, "
                "payload, written_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    prev,
                    p_hash,
                    e_hash,
                    kind.value if isinstance(kind, LedgerKind) else str(kind),
                    canonical_json(payload),
                    stamped.isoformat(),
                ),
            )
            self._conn.commit()

        return LedgerEntry(
            seq=seq,
            prev_hash=prev,
            payload_hash=p_hash,
            kind=kind,
            payload=payload,
            written_at=stamped,
        )

    def record_action(self, action: ActionRecord) -> None:
        """The `LedgerSink` protocol `PolicyGate` writes through.

        Refusals land here too — `blocked=True` with a reason — because an injection
        that is silently dropped is indistinguishable from one that never happened.
        """
        self.append(LedgerKind.ACTION, action)

    def extend(self, kind: LedgerKind, payloads: Iterable[Any]) -> list[LedgerEntry]:
        return [self.append(kind, payload) for payload in payloads]

    # ------------------------------------------------------------- reading

    def __len__(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])

    def __iter__(self) -> Iterator[LedgerEntry]:
        return iter(self.entries())

    def entries(
        self, *, kind: LedgerKind | None = None, limit: int | None = None
    ) -> list[LedgerEntry]:
        sql = "SELECT * FROM ledger"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind.value)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_row_to_entry(row) for row in self._conn.execute(sql, params)]

    def head(self) -> str:
        """Hash of the last entry. Publish this and the whole chain becomes checkable."""
        row = self._conn.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["entry_hash"] if row else GENESIS

    def find(self, **fields: Any) -> list[LedgerEntry]:
        """Entries whose payload matches every given field.

        Deliberately a scan over decoded payloads rather than SQL over JSON columns:
        the payload shapes differ by kind, and a query language over a union type is
        how you get silently-empty results.
        """
        return [
            entry
            for entry in self.entries()
            if all(entry.payload.get(key) == value for key, value in fields.items())
        ]

    # -------------------------------------------------------- verification

    def verify_chain(self) -> VerificationResult:
        return verify_entries(self.entries())

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ testing

    def _disable_append_only_guards(self) -> None:
        """Remove the triggers so a test can actually tamper.

        Named to be conspicuous in a grep. The tamper-detection test has to do real
        damage or it proves nothing, and a control that can only be tested by
        pretending it works is not a control.
        """
        self._conn.executescript(_DROP_TRIGGERS)
        self._conn.commit()


def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        seq=row["seq"],
        prev_hash=row["prev_hash"],
        payload_hash=row["payload_hash"],
        kind=LedgerKind(row["kind"]),
        payload=json.loads(row["payload"]),
        written_at=datetime.fromisoformat(row["written_at"]),
    )


def open_ledger(config=None) -> Ledger:
    """The configured ledger. `storage.database_url` names a SQLite file."""
    from antar.config import get_config, repo_root

    config = config or get_config()
    url = str(config.get("storage.database_url", "sqlite:///artifacts/antar.db"))
    if not url.startswith("sqlite:///"):
        raise ValueError(
            f"the ledger supports SQLite only; got {url!r}. Postgres is the "
            "docker-compose path and is not wired to the ledger (LIMITATIONS)."
        )
    relative = url.removeprefix("sqlite:///")
    return Ledger(repo_root() / relative)


__all__ = [
    "GENESIS",
    "CanonicalisationError",
    "ChainBreak",
    "Ledger",
    "VerificationResult",
    "canonical_json",
    "entry_hash",
    "open_ledger",
    "payload_hash",
    "verify_entries",
]
