"""Durable paper-entry intents; no broker calls or reconstructed cash ledger.

Commit a claim BEFORE a future adapter sends anything. A claimed intent is
never automatically dispatchable again, even when a lookup finds no order.
SQLite transactions protect local claims, not exactly-once external execution.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from orb.breakout import BreakoutDetector
from orb.domain import BreakoutSignal
from orb.session import exchange_session
from orb.strategy import NEW_YORK


class JournalError(ValueError):
    """A durable-state conflict or invalid lifecycle observation."""


def _stamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise JournalError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _token(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None:
        raise JournalError("identity must be 1–128 ASCII identifier characters")


@dataclass(frozen=True)
class EntryIntent:
    signal_id: str
    strategy_version: str
    symbol: str
    session: str
    bar_start: str
    observed_at: str
    entry_reference: str
    stop: str
    target: str
    quantity: int
    context_id: str

    @classmethod
    def from_signal(cls, signal: BreakoutSignal, quantity: int, context_id: str):
        if type(quantity) is not int or not 1 <= quantity <= 2_147_483_647:
            raise JournalError("quantity must be a positive bounded integer")
        if not isinstance(context_id, str) or re.fullmatch(r"[0-9a-f]{64}", context_id) is None:
            raise JournalError("context_id must be a SHA-256 configuration/evidence identity")
        bar_start, observed_at = _stamp(signal.bar_start), _stamp(signal.observed_at)
        day = signal.bar_start.astimezone(NEW_YORK).date()
        session = exchange_session(day)
        if (signal.strategy_version != BreakoutDetector.VERSION or signal.symbol not in ("SPY", "QQQ")
                or session is None or signal.bar_start.second or signal.bar_start.microsecond
                or not session.range_end <= signal.bar_start
                or not signal.bar_start + timedelta(minutes=1) <= signal.observed_at < session.entry_cutoff
                or signal.observed_at - signal.bar_start > timedelta(seconds=150)):
            raise JournalError("intent violates completed-minute baseline")
        identity = f"{signal.strategy_version}|{signal.symbol}|{day}|1"
        if signal.signal_id != sha256(identity.encode()).hexdigest()[:32]:
            raise JournalError("signal identity does not match session attempt")
        prices = (signal.entry_reference, signal.stop, signal.target)
        if (any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in prices)
                or signal.stop >= signal.entry_reference
                or signal.target != signal.entry_reference + 2 * (signal.entry_reference - signal.stop)):
            raise JournalError("intent requires positive long prices and planned 2R target")
        return cls(signal.signal_id, signal.strategy_version, signal.symbol, day.isoformat(),
                   bar_start, observed_at, *(str(p) for p in prices), quantity, context_id)

    def validated(self):
        """Revalidate even directly constructed/deserialised intents."""
        try:
            signal = BreakoutSignal(self.signal_id, self.strategy_version, self.symbol,
                                    datetime.fromisoformat(self.bar_start), datetime.fromisoformat(self.observed_at),
                                    Decimal(self.entry_reference), Decimal(self.stop), Decimal(self.target))
            canonical = self.from_signal(signal, self.quantity, self.context_id)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise JournalError("invalid entry intent") from exc
        if canonical != self:
            raise JournalError("intent fields must be canonical and match session")
        return self

    @property
    def client_order_id(self):
        return "orb-" + self.signal_id


TERMINAL = frozenset(("filled", "cancelled", "rejected", "expired"))
OBSERVATIONS = frozenset(("accepted", "partial", *TERMINAL))


class IntentJournal:
    """One account-scoped SQLite file on a local filesystem.

    FULL synchronous rollback-journal transactions; safe concurrent reservations
    and claims with optimistic revisions. Do not share a connection across
    threads. Recovery is explicit and requires the old dispatcher to be stopped.
    Files are operational evidence, not an authenticated/tamper-proof audit log.
    """

    def __init__(self, path: Path, account_scope: str, *, create: bool = True):
        _token(account_scope)
        if not account_scope.startswith("paper:") or account_scope == "paper:":
            raise JournalError("journal requires an explicit paper account scope")
        self.account_scope = account_scope
        path = Path(path)
        if str(path) == ":memory:" or path.is_symlink():
            raise JournalError("journal requires a durable non-symlink path")
        self._db = sqlite3.connect(path.resolve().as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                                   uri=True, timeout=5, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            if self._db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise JournalError("journal requires SQLite rollback-journal mode")
            with self._transaction():
                tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    if not create:
                        raise JournalError("existing file has no journal schema")
                    self._db.execute("CREATE TABLE metadata (version INTEGER NOT NULL, account_scope TEXT NOT NULL)")
                    self._db.execute("INSERT INTO metadata VALUES (1, ?)", (account_scope,))
                    self._db.execute("""CREATE TABLE intents (
                        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, session TEXT NOT NULL,
                        payload TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                        filled_quantity INTEGER NOT NULL, filled_notional TEXT NOT NULL,
                        broker_order_id TEXT UNIQUE, updated_at TEXT NOT NULL,
                        UNIQUE(symbol, session))""")
                    self._db.execute("""CREATE TABLE events (
                        sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                        signal_id TEXT NOT NULL REFERENCES intents(signal_id),
                        payload TEXT NOT NULL, observed_at TEXT NOT NULL)""")
                if tables and tables != {"metadata", "intents", "events"}:
                    raise JournalError("unrecognised journal schema")
                rows = self._db.execute("SELECT version, account_scope FROM metadata").fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (1, account_scope):
                    raise JournalError("journal version or paper account scope mismatch")
                if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise JournalError("journal integrity check failed")
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _row(self, signal_id):
        row = self._db.execute("SELECT * FROM intents WHERE signal_id=?", (signal_id,)).fetchone()
        if row is None:
            raise JournalError("unknown intent")
        return dict(row)

    def get(self, signal_id):
        return self._row(signal_id)

    def _event(self, event_id, signal_id, payload, observed_at):
        self._db.execute("INSERT INTO events(event_id,signal_id,payload,observed_at) VALUES (?,?,?,?)",
                         (event_id, signal_id, payload, observed_at))

    def prepare(self, intent: EntryIntent, at: datetime):
        intent.validated()
        stamp = _stamp(at)
        if stamp < intent.observed_at:
            raise JournalError("cannot prepare before signal observation")
        payload = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":"))
        with self._transaction():
            row = self._db.execute("SELECT * FROM intents WHERE symbol=? AND session=?",
                                   (intent.symbol, intent.session)).fetchone()
            if row is not None:
                if row["payload"] != payload:
                    raise JournalError("session attempt already reserved with different intent")
                return dict(row)
            self._db.execute("INSERT INTO intents VALUES (?,?,?,?,?,0,0,'0',NULL,?)",
                             (intent.signal_id, intent.symbol, intent.session, payload, "prepared", stamp))
            self._event("prepare:" + intent.signal_id, intent.signal_id, payload, stamp)
            return self._row(intent.signal_id)

    def _change(self, row, state, stamp, expected_revision):
        if type(expected_revision) is not int or row["revision"] != expected_revision:
            raise JournalError("stale intent revision")
        if stamp < row["updated_at"]:
            raise JournalError("event clock moved backwards")
        self._db.execute("UPDATE intents SET state=?,revision=revision+1,updated_at=? WHERE signal_id=?",
                         (state, stamp, row["signal_id"]))

    def claim(self, signal_id: str, *, expected_revision: int, at: datetime):
        """Commit once before sending; a repeated claim always fails closed."""
        stamp = _stamp(at)
        with self._transaction():
            row = self._row(signal_id)
            if row["state"] != "prepared":
                raise JournalError("intent is not dispatchable; reconcile instead of resending")
            intent = json.loads(row["payload"])
            bar_start = datetime.fromisoformat(intent["bar_start"])
            session = exchange_session(bar_start.astimezone(NEW_YORK).date())
            if at >= session.entry_cutoff or at - bar_start > timedelta(seconds=150):
                raise JournalError("entry intent is stale or past cutoff; do not submit")
            self._change(row, "dispatching", stamp, expected_revision)
            self._event("claim:" + signal_id, signal_id, '{"state":"dispatching"}', stamp)
            return self._row(signal_id)

    def recover(self, at: datetime):
        """After old dispatchers stop, latch interrupted claims as unknown.

        Prepared intents remain evidence for the future coordinator to expire
        or revalidate, never an instruction to submit stale signals on restart.
        """
        stamp = _stamp(at)
        with self._transaction():
            rows = self._db.execute("SELECT * FROM intents WHERE state='dispatching'").fetchall()
            for row in rows:
                self._change(row, "unknown", stamp, row["revision"])
                self._event("recover:" + row["signal_id"], row["signal_id"], '{"state":"unknown"}', stamp)
        return self.snapshot()

    def observe(self, signal_id: str, *, event_id: str, state: str, filled_quantity: int,
                filled_notional: Decimal, broker_order_id: str, expected_revision: int, at: datetime):
        """Apply adapter-normalised cumulative evidence, not incremental fills.

        A missing lookup is NOT a rejection. Cancellation/expiry can retain
        filled shares. Terminal amendments/conflicting duplicate evidence fail
        closed for operator reconciliation; this is not the broker adapter.
        """
        _token(event_id)
        _token(broker_order_id)
        stamp = _stamp(at)
        if state not in OBSERVATIONS or type(filled_quantity) is not int or filled_quantity < 0:
            raise JournalError("invalid broker lifecycle observation")
        if (not isinstance(filled_notional, Decimal) or not filled_notional.is_finite()
                or filled_notional < 0 or (filled_quantity == 0) != (filled_notional == 0)):
            raise JournalError("cumulative fill quantity/notional mismatch")
        payload = json.dumps({"state": state, "filled_quantity": filled_quantity,
                              "filled_notional": str(filled_notional), "broker_order_id": broker_order_id},
                             sort_keys=True, separators=(",", ":"))
        with self._transaction():
            old = self._db.execute("SELECT * FROM events WHERE event_id=?", ("broker:" + event_id,)).fetchone()
            if old is not None:
                if old["signal_id"] != signal_id or old["payload"] != payload:
                    raise JournalError("conflicting duplicate broker event")
                return self._row(signal_id)  # Exact retry, even after newer events.
            row = self._row(signal_id)
            quantity = json.loads(row["payload"])["quantity"]
            if row["state"] == "prepared" or row["state"] in TERMINAL:
                raise JournalError("observation conflicts with intent lifecycle")
            if row["broker_order_id"] not in (None, broker_order_id):
                raise JournalError("broker order identity changed")
            old_quantity, old_notional = row["filled_quantity"], Decimal(row["filled_notional"])
            if (not old_quantity <= filled_quantity <= quantity or filled_notional < old_notional
                    or (filled_quantity == old_quantity and filled_notional != old_notional)
                    or (filled_quantity > old_quantity and filled_notional <= old_notional)
                    or (state == "accepted" and filled_quantity != 0)
                    or (state == "partial" and not 0 < filled_quantity < quantity)
                    or (state == "filled" and filled_quantity != quantity)
                    or (state == "rejected" and filled_quantity != 0)
                    or (state in ("cancelled", "expired") and filled_quantity == quantity)):
                raise JournalError("inconsistent cumulative fill or lifecycle regression")
            self._change(row, state, stamp, expected_revision)
            self._db.execute("""UPDATE intents SET filled_quantity=?,filled_notional=?,broker_order_id=?
                                WHERE signal_id=?""", (filled_quantity, str(filled_notional), broker_order_id, signal_id))
            self._event("broker:" + event_id, signal_id, payload, stamp)
            return self._row(signal_id)

    def snapshot(self):
        # One read transaction prevents mixed versions if another connection writes.
        self._db.execute("BEGIN")
        try:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM intents ORDER BY session,symbol")]
            events = [dict(r) for r in self._db.execute("SELECT * FROM events ORDER BY sequence")]
            return {"schema_version": 1, "account_scope": self.account_scope, "intents": rows, "events": events,
                    "reconciliation_required": [r["signal_id"] for r in rows
                                                if r["state"] not in TERMINAL and r["state"] != "prepared"],
                    "filled_shares": {r["signal_id"]: r["filled_quantity"] for r in rows if r["filled_quantity"]},
                    "automatic_resubmission": False, "cash_ledger_restored": False,
                    "prepared_requires_fresh_admission": True}
        finally:
            self._db.execute("ROLLBACK")


def recovery_demo(path: Path):
    """Invented partial-cancel and interrupted-submit exercise; no network."""
    # Refuse overwrite even if a caller points at an unrelated/empty file.
    with Path(path).open("xb"):
        pass
    at = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
    intents = []
    for symbol in ("SPY", "QQQ"):
        identity = f"{BreakoutDetector.VERSION}|{symbol}|2026-10-09|1"
        signal = BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], BreakoutDetector.VERSION,
                                symbol, at - timedelta(minutes=1), at,
                                Decimal("100.20"), Decimal("99"), Decimal("102.60"))
        intents.append(EntryIntent.from_signal(signal, 10, sha256(b"synthetic-recovery-demo-v1").hexdigest()))
    with IntentJournal(path, "paper:synthetic-demo") as journal:
        for intent in intents:
            journal.prepare(intent, at)
            journal.claim(intent.signal_id, expected_revision=0, at=at)
        journal.observe(intents[0].signal_id, event_id="partial-1", state="partial",
                        filled_quantity=2, filled_notional=Decimal("200.40"), broker_order_id="synthetic-spy",
                        expected_revision=1, at=at)
        journal.observe(intents[0].signal_id, event_id="cancel-1", state="cancelled",
                        filled_quantity=2, filled_notional=Decimal("200.40"), broker_order_id="synthetic-spy",
                        expected_revision=2, at=at)
    with IntentJournal(path, "paper:synthetic-demo", create=False) as journal:
        result = journal.recover(at + timedelta(seconds=1))
    return {"synthetic": True, "orders_submitted": False, **result}
