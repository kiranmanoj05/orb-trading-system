"""Durable, offline reconstruction of partial paper fills and exits.

This module accepts adapter-normalised cumulative evidence.  It has no network
client and no order-submission method.  A complete broker snapshot can clear
the recovery gate only when cash, positions and open client-order identities
exactly agree with the replayed model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from orb.journal import EntryIntent, JournalError, _stamp, _token


ZERO = Decimal("0")
CONTEXT = Context(prec=28)
ENTRY_STATES = frozenset(("accepted", "partial", "filled", "cancelled", "rejected", "expired", "unknown"))
TERMINAL = frozenset(("filled", "cancelled", "rejected", "expired"))
ACTIVE = frozenset(("accepted", "partial"))


class ReconciliationError(ValueError):
    """Invalid evidence, replay conflict, or unsafe recovery state."""


def _money(name: str, value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ReconciliationError(f"{name} must be a {'positive' if positive else 'non-negative'} finite Decimal")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _model_id() -> str:
    return sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int

    def validated(self) -> "BrokerPosition":
        if self.symbol not in ("SPY", "QQQ") or type(self.quantity) is not int or self.quantity <= 0:
            raise ReconciliationError("broker positions require positive whole SPY/QQQ shares")
        return self


@dataclass(frozen=True)
class BrokerSnapshot:
    account_scope: str
    as_of: datetime
    cash: Decimal
    positions: tuple[BrokerPosition, ...]
    open_client_order_ids: tuple[str, ...]
    complete: bool

    def payload(self) -> dict:
        _token(self.account_scope)
        if not self.account_scope.startswith("paper:") or self.account_scope == "paper:":
            raise ReconciliationError("snapshot requires an explicit paper account scope")
        stamp = _stamp(self.as_of)
        _money("cash", self.cash)
        if type(self.complete) is not bool:
            raise ReconciliationError("snapshot completeness must be boolean")
        positions: dict[str, int] = {}
        for item in self.positions:
            item.validated()
            if item.symbol in positions:
                raise ReconciliationError("duplicate broker position symbol")
            positions[item.symbol] = item.quantity
        ids = []
        for value in self.open_client_order_ids:
            _token(value)
            if value in ids:
                raise ReconciliationError("duplicate open client order identity")
            ids.append(value)
        return {"account_scope": self.account_scope, "as_of": stamp, "cash": str(self.cash),
                "positions": positions, "open_client_order_ids": sorted(ids), "complete": self.complete}


@dataclass
class ExitOrder:
    client_order_id: str
    quantity: int
    state: str = "pending"
    broker_order_id: str | None = None
    filled_quantity: int = 0
    filled_notional: Decimal = ZERO
    fees: Decimal = ZERO


@dataclass
class PositionRecord:
    intent: EntryIntent
    reserved_cash: Decimal
    planned_stop_risk: Decimal
    entry_state: str = "pending"
    broker_entry_order_id: str | None = None
    entry_quantity: int = 0
    entry_notional: Decimal = ZERO
    entry_fees: Decimal = ZERO
    exits: list[ExitOrder] | None = None

    def __post_init__(self):
        if self.exits is None:
            self.exits = []

    @property
    def exited_quantity(self) -> int:
        return sum(x.filled_quantity for x in self.exits)

    @property
    def open_quantity(self) -> int:
        return self.entry_quantity - self.exited_quantity


class _Model:
    def __init__(self, initial_cash: Decimal, account_scope: str):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.account_scope = account_scope
        self.records: dict[str, PositionRecord] = {}
        self.last_at: str | None = None
        self.last_reconciliation: dict | None = None
        self.last_reconciliation_sequence: int | None = None

    def _clock(self, at: str) -> None:
        try:
            value = datetime.fromisoformat(at)
        except (TypeError, ValueError) as exc:
            raise ReconciliationError("invalid event timestamp") from exc
        stamp = _stamp(value)
        if stamp != at:
            raise ReconciliationError("event timestamps must be canonical UTC")
        if self.last_at is not None and stamp < self.last_at:
            raise ReconciliationError("event clock moved backwards")
        self.last_at = stamp

    def apply(self, kind: str, payload: dict, sequence: int) -> dict:
        at = payload.get("at")
        if kind != "reconcile":
            self._clock(at)
            self.last_reconciliation = None
            self.last_reconciliation_sequence = None
        if kind == "register":
            outcome = self._register(payload)
        elif kind == "observe_entry":
            outcome = self._observe_entry(payload)
        elif kind == "open_exit":
            outcome = self._open_exit(payload)
        elif kind == "observe_exit":
            outcome = self._observe_exit(payload)
        elif kind == "reconcile":
            outcome = self._reconcile(payload, sequence)
        else:
            raise ReconciliationError("unknown reconciliation command")
        return outcome

    def _record(self, signal_id: str) -> PositionRecord:
        try:
            return self.records[signal_id]
        except KeyError as exc:
            raise ReconciliationError("unknown entry intent") from exc

    def _register(self, payload: dict) -> dict:
        try:
            intent = EntryIntent(**payload["intent"]).validated()
            reserved_cash = Decimal(payload["reserved_cash"])
            planned_risk = Decimal(payload["planned_stop_risk"])
        except (KeyError, TypeError, ValueError, ArithmeticError, JournalError) as exc:
            raise ReconciliationError("invalid registered entry intent") from exc
        _money("reserved cash", reserved_cash, positive=True)
        _money("planned stop risk", planned_risk, positive=True)
        if intent.signal_id in self.records:
            raise ReconciliationError("entry intent already registered")
        if any(r.intent.symbol == intent.symbol and (r.open_quantity > 0 or r.entry_state not in TERMINAL)
               for r in self.records.values()):
            raise ReconciliationError("symbol already has an open reconstructed position")
        if reserved_cash > self.available_cash:
            raise ReconciliationError("registered reservation exceeds available cash")
        self.records[intent.signal_id] = PositionRecord(intent, reserved_cash, planned_risk)
        return {"signal_id": intent.signal_id, "client_order_id": intent.client_order_id,
                "state": "pending", "orders_submitted": False}

    @staticmethod
    def _cumulative(payload: dict, maximum: int) -> tuple[int, Decimal, Decimal]:
        try:
            quantity = payload["filled_quantity"]
            notional = Decimal(payload["filled_notional"])
            fees = Decimal(payload["fees"])
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ReconciliationError("invalid cumulative fill evidence") from exc
        if type(quantity) is not int or not 0 <= quantity <= maximum:
            raise ReconciliationError("cumulative filled quantity is out of bounds")
        _money("filled notional", notional)
        _money("fees", fees)
        if (quantity == 0) != (notional == 0) or (quantity == 0 and fees != 0):
            raise ReconciliationError("cumulative quantity/notional/fees mismatch")
        return quantity, notional, fees

    @staticmethod
    def _transition(old_state: str, state: str, old_q: int, q: int, maximum: int,
                    old_notional: Decimal, notional: Decimal, old_fees: Decimal, fees: Decimal) -> None:
        if state not in ENTRY_STATES or old_state in TERMINAL or old_state == "unknown":
            raise ReconciliationError("lifecycle observation conflicts with terminal or unknown state")
        if q < old_q or notional < old_notional or fees < old_fees:
            raise ReconciliationError("cumulative fill evidence moved backwards")
        if q == old_q and (notional != old_notional or fees != old_fees):
            raise ReconciliationError("fill totals changed without new shares")
        if q > old_q and notional <= old_notional:
            raise ReconciliationError("new filled shares require increased notional")
        if ((state == "accepted" and q != 0) or (state == "partial" and not 0 < q < maximum)
                or (state == "filled" and q != maximum) or (state == "rejected" and q != 0)
                or (state in ("cancelled", "expired") and q == maximum)
                or (state == "unknown" and q != old_q)):
            raise ReconciliationError("state disagrees with cumulative fill evidence")

    def _observe_entry(self, payload: dict) -> dict:
        record = self._record(payload.get("signal_id"))
        state = payload.get("state")
        broker_id = payload.get("broker_order_id")
        _token(broker_id)
        if record.broker_entry_order_id not in (None, broker_id):
            raise ReconciliationError("broker entry order identity changed")
        quantity, notional, fees = self._cumulative(payload, record.intent.quantity)
        self._transition(record.entry_state, state, record.entry_quantity, quantity, record.intent.quantity,
                         record.entry_notional, notional, record.entry_fees, fees)
        delta = notional - record.entry_notional + fees - record.entry_fees
        if delta > self.cash:
            raise ReconciliationError("entry evidence would make modeled cash negative")
        self.cash -= delta
        record.entry_state, record.broker_entry_order_id = state, broker_id
        record.entry_quantity, record.entry_notional, record.entry_fees = quantity, notional, fees
        return self._record_dict(record)

    def _open_exit(self, payload: dict) -> dict:
        record = self._record(payload.get("signal_id"))
        client_id = payload.get("client_order_id")
        quantity = payload.get("quantity")
        _token(client_id)
        if record.entry_state not in TERMINAL or record.entry_quantity == 0:
            raise ReconciliationError("exit planning requires terminal entry evidence with filled shares")
        if any(x.state not in TERMINAL for x in record.exits):
            raise ReconciliationError("an exit order is already active or unknown")
        if any(x.client_order_id == client_id for r in self.records.values() for x in r.exits):
            raise ReconciliationError("exit client order identity already consumed")
        if type(quantity) is not int or not 1 <= quantity <= record.open_quantity:
            raise ReconciliationError("exit quantity exceeds the open position")
        record.exits.append(ExitOrder(client_id, quantity))
        return {"signal_id": record.intent.signal_id, "client_order_id": client_id,
                "quantity": quantity, "state": "pending", "orders_submitted": False}

    def _observe_exit(self, payload: dict) -> dict:
        record = self._record(payload.get("signal_id"))
        client_id, state, broker_id = payload.get("client_order_id"), payload.get("state"), payload.get("broker_order_id")
        _token(client_id)
        _token(broker_id)
        try:
            order = next(x for x in record.exits if x.client_order_id == client_id)
        except StopIteration as exc:
            raise ReconciliationError("unknown exit order") from exc
        if order.broker_order_id not in (None, broker_id):
            raise ReconciliationError("broker exit order identity changed")
        quantity, notional, fees = self._cumulative(payload, order.quantity)
        self._transition(order.state, state, order.filled_quantity, quantity, order.quantity,
                         order.filled_notional, notional, order.fees, fees)
        delta_quantity = quantity - order.filled_quantity
        if delta_quantity > record.open_quantity:
            raise ReconciliationError("exit evidence exceeds the reconstructed position")
        self.cash += notional - order.filled_notional - (fees - order.fees)
        order.state, order.broker_order_id = state, broker_id
        order.filled_quantity, order.filled_notional, order.fees = quantity, notional, fees
        return self._record_dict(record)

    @property
    def reserved_cash(self) -> Decimal:
        total = ZERO
        for record in self.records.values():
            if record.entry_state not in TERMINAL and record.entry_state != "unknown":
                remaining = record.intent.quantity - record.entry_quantity
                total += record.reserved_cash * Decimal(remaining) / Decimal(record.intent.quantity)
        return total

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    @property
    def committed_stop_risk(self) -> Decimal:
        total = ZERO
        for record in self.records.values():
            if record.entry_state == "pending":
                total += record.planned_stop_risk
                continue
            if record.entry_state not in TERMINAL and record.entry_state != "unknown":
                planned = record.planned_stop_risk
            elif record.entry_quantity:
                planned = record.planned_stop_risk * Decimal(record.open_quantity) / Decimal(record.intent.quantity)
            else:
                planned = ZERO
            if record.entry_quantity and record.open_quantity:
                fraction = Decimal(record.open_quantity) / Decimal(record.entry_quantity)
                actual = ((record.entry_notional + record.entry_fees) * fraction
                          - Decimal(record.intent.stop) * record.open_quantity)
                planned = max(planned, actual, ZERO)
            total += planned
        return total

    @property
    def gross_exposure(self) -> Decimal:
        return sum((r.entry_notional * Decimal(r.open_quantity) / Decimal(r.entry_quantity)
                    for r in self.records.values() if r.entry_quantity and r.open_quantity), ZERO)

    @property
    def realized_pnl(self) -> Decimal:
        total = ZERO
        for record in self.records.values():
            if not record.entry_quantity:
                continue
            q = record.exited_quantity
            proceeds = sum((x.filled_notional - x.fees for x in record.exits), ZERO)
            basis = (record.entry_notional + record.entry_fees) * Decimal(q) / Decimal(record.entry_quantity)
            total += proceeds - basis
        return total

    def _expected_positions(self) -> dict[str, int]:
        positions: dict[str, int] = {}
        for record in self.records.values():
            if record.open_quantity:
                positions[record.intent.symbol] = positions.get(record.intent.symbol, 0) + record.open_quantity
        return positions

    def _expected_open_orders(self) -> list[str]:
        result = []
        for record in self.records.values():
            if record.entry_state in ACTIVE:
                result.append(record.intent.client_order_id)
            result.extend(x.client_order_id for x in record.exits if x.state in ACTIVE)
        return sorted(result)

    def _reconcile(self, payload: dict, sequence: int) -> dict:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ReconciliationError("invalid broker snapshot payload")
        at = snapshot.get("as_of")
        self._clock(at)
        reasons = []
        if snapshot.get("account_scope") != self.account_scope:
            reasons.append("account_scope_mismatch")
        if snapshot.get("complete") is not True:
            reasons.append("snapshot_incomplete")
        if Decimal(snapshot.get("cash", "NaN")) != self.cash:
            reasons.append("cash_mismatch")
        if snapshot.get("positions") != self._expected_positions():
            reasons.append("position_mismatch")
        if snapshot.get("open_client_order_ids") != self._expected_open_orders():
            reasons.append("open_order_mismatch")
        if any(r.entry_state == "unknown" or any(x.state == "unknown" for x in r.exits)
               for r in self.records.values()):
            reasons.append("unknown_lifecycle")
        outcome = {"broker_reconciled": not reasons, "paper_execution_enabled": False,
                   "reasons": reasons, "snapshot_as_of": at}
        self.last_reconciliation = outcome
        self.last_reconciliation_sequence = sequence
        return outcome

    @staticmethod
    def _record_dict(record: PositionRecord) -> dict:
        return {"signal_id": record.intent.signal_id, "symbol": record.intent.symbol,
                "entry_state": record.entry_state, "entry_quantity": record.entry_quantity,
                "entry_notional": str(record.entry_notional), "entry_fees": str(record.entry_fees),
                "open_quantity": record.open_quantity,
                "exits": [{"client_order_id": x.client_order_id, "quantity": x.quantity, "state": x.state,
                           "filled_quantity": x.filled_quantity, "filled_notional": str(x.filled_notional),
                           "fees": str(x.fees), "broker_order_id": x.broker_order_id} for x in record.exits]}

    def snapshot(self, sequence: int) -> dict:
        reconciled = bool(self.last_reconciliation and self.last_reconciliation["broker_reconciled"]
                          and self.last_reconciliation_sequence == sequence)
        return {"model_version": "partial-fill-reconciliation-v1", "account_scope": self.account_scope,
                "initial_cash": str(self.initial_cash), "cash": str(self.cash),
                "reserved_cash": str(self.reserved_cash), "available_cash": str(self.available_cash),
                "gross_exposure": str(self.gross_exposure),
                "committed_stop_risk": str(self.committed_stop_risk),
                "realized_pnl": str(self.realized_pnl),
                "positions": self._expected_positions(), "open_client_order_ids": self._expected_open_orders(),
                "records": [self._record_dict(r) for r in self.records.values()],
                "last_reconciliation": self.last_reconciliation,
                "broker_reconciled": reconciled, "paper_execution_enabled": False,
                "orders_submitted": False, "event_count": sequence}


class ReconciliationLedger:
    """Account-scoped SQLite command log replaying normalized broker evidence."""

    def __init__(self, path: Path, account_scope: str, initial_cash: Decimal, *, create: bool = True):
        _token(account_scope)
        if not account_scope.startswith("paper:") or account_scope == "paper:":
            raise ReconciliationError("ledger requires an explicit paper account scope")
        _money("initial cash", initial_cash, positive=True)
        path = Path(path)
        if str(path) == ":memory:" or path.is_symlink():
            raise ReconciliationError("ledger requires a durable non-symlink path")
        self.account_scope, self.initial_cash = account_scope, initial_cash
        self._db = sqlite3.connect(path.resolve().as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                                   uri=True, timeout=5, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA synchronous=FULL")
            if self._db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ReconciliationError("ledger requires SQLite rollback-journal mode")
            self._db.execute("BEGIN IMMEDIATE")
            tables = {x[0] for x in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                if not create:
                    raise ReconciliationError("existing file has no reconciliation schema")
                self._db.execute("CREATE TABLE metadata(version INTEGER NOT NULL, account_scope TEXT NOT NULL, initial_cash TEXT NOT NULL, model_id TEXT NOT NULL)")
                self._db.execute("INSERT INTO metadata VALUES(1,?,?,?)", (account_scope, str(initial_cash), _model_id()))
                self._db.execute("CREATE TABLE commands(sequence INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT NOT NULL)")
            elif tables != {"metadata", "commands"}:
                raise ReconciliationError("unrecognised reconciliation schema")
            rows = self._db.execute("SELECT * FROM metadata").fetchall()
            if (len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["account_scope"] != account_scope
                    or rows[0]["initial_cash"] != str(initial_cash) or rows[0]["model_id"] != _model_id()):
                raise ReconciliationError("reconciliation version, configuration or model mismatch")
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ReconciliationError("reconciliation integrity check failed")
            self._db.execute("COMMIT")
            self._replay()
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _replay(self) -> _Model:
        model = _Model(self.initial_cash, self.account_scope)
        with localcontext(CONTEXT):
            for row in self._db.execute("SELECT * FROM commands ORDER BY sequence"):
                payload = json.loads(row["payload"])
                outcome = model.apply(row["kind"], payload, row["sequence"])
                if _canonical(outcome) != row["outcome"]:
                    raise ReconciliationError("saved reconciliation outcome failed deterministic replay")
        return model

    def _command(self, request_id: str, kind: str, payload: dict) -> dict:
        _token(request_id)
        encoded = _canonical(payload)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            old = self._db.execute("SELECT * FROM commands WHERE request_id=?", (request_id,)).fetchone()
            if old is not None:
                if old["kind"] != kind or old["payload"] != encoded:
                    raise ReconciliationError("request identity was reused with different input")
                result = json.loads(old["outcome"])
                self._db.execute("COMMIT")
                return result
            model = self._replay()
            sequence = self._db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM commands").fetchone()[0]
            with localcontext(CONTEXT):
                result = model.apply(kind, payload, sequence)
            self._db.execute("INSERT INTO commands VALUES(?,?,?,?,?)",
                             (sequence, request_id, kind, encoded, _canonical(result)))
            self._db.execute("COMMIT")
            return result
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def register(self, request_id: str, intent: EntryIntent, *, reserved_cash: Decimal,
                 planned_stop_risk: Decimal, at: datetime) -> dict:
        intent.validated()
        _money("reserved cash", reserved_cash, positive=True)
        _money("planned stop risk", planned_stop_risk, positive=True)
        return self._command(request_id, "register", {"intent": asdict(intent),
                             "reserved_cash": str(reserved_cash), "planned_stop_risk": str(planned_stop_risk),
                             "at": _stamp(at)})

    def observe_entry(self, request_id: str, signal_id: str, *, state: str, filled_quantity: int,
                      filled_notional: Decimal, fees: Decimal, broker_order_id: str, at: datetime) -> dict:
        return self._command(request_id, "observe_entry", {"signal_id": signal_id, "state": state,
            "filled_quantity": filled_quantity, "filled_notional": str(filled_notional), "fees": str(fees),
            "broker_order_id": broker_order_id, "at": _stamp(at)})

    def open_exit(self, request_id: str, signal_id: str, *, client_order_id: str,
                  quantity: int, at: datetime) -> dict:
        return self._command(request_id, "open_exit", {"signal_id": signal_id,
            "client_order_id": client_order_id, "quantity": quantity, "at": _stamp(at)})

    def observe_exit(self, request_id: str, signal_id: str, *, client_order_id: str, state: str,
                     filled_quantity: int, filled_notional: Decimal, fees: Decimal,
                     broker_order_id: str, at: datetime) -> dict:
        return self._command(request_id, "observe_exit", {"signal_id": signal_id,
            "client_order_id": client_order_id, "state": state, "filled_quantity": filled_quantity,
            "filled_notional": str(filled_notional), "fees": str(fees),
            "broker_order_id": broker_order_id, "at": _stamp(at)})

    def reconcile(self, request_id: str, snapshot: BrokerSnapshot) -> dict:
        return self._command(request_id, "reconcile", {"snapshot": snapshot.payload()})

    def snapshot(self) -> dict:
        self._db.execute("BEGIN")
        try:
            model = self._replay()
            sequence = self._db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            return model.snapshot(sequence)
        finally:
            self._db.execute("ROLLBACK")


def reconciliation_demo(path: Path) -> dict:
    """Invented partial-entry, partial-exit and exact-snapshot restart exercise."""
    with Path(path).open("xb"):
        pass
    at = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
    from orb.breakout import BreakoutDetector
    from orb.domain import BreakoutSignal
    identity = f"{BreakoutDetector.VERSION}|SPY|2026-10-09|1"
    signal = BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], BreakoutDetector.VERSION, "SPY",
                            at - timedelta(minutes=1), at, Decimal("100.20"), Decimal("99"), Decimal("102.60"))
    intent = EntryIntent.from_signal(signal, 4, sha256(b"synthetic-partial-reconciliation-v1").hexdigest())
    with ReconciliationLedger(path, "paper:synthetic-reconciliation", Decimal("10000")) as ledger:
        ledger.register("register", intent, reserved_cash=Decimal("400.84"),
                        planned_stop_risk=Decimal("4.88"), at=at)
        ledger.observe_entry("entry-partial", intent.signal_id, state="partial", filled_quantity=2,
                             filled_notional=Decimal("200.40"), fees=Decimal("0.02"),
                             broker_order_id="broker-entry", at=at)
        ledger.observe_entry("entry-cancel", intent.signal_id, state="cancelled", filled_quantity=2,
                             filled_notional=Decimal("200.40"), fees=Decimal("0.02"),
                             broker_order_id="broker-entry", at=at)
        ledger.open_exit("open-exit", intent.signal_id, client_order_id="orb-exit-demo-1", quantity=2, at=at)
        ledger.observe_exit("exit-partial", intent.signal_id, client_order_id="orb-exit-demo-1", state="partial",
                            filled_quantity=1, filled_notional=Decimal("101"), fees=Decimal("0.01"),
                            broker_order_id="broker-exit", at=at)
    with ReconciliationLedger(path, "paper:synthetic-reconciliation", Decimal("10000"), create=False) as ledger:
        before = ledger.snapshot()
        result = ledger.reconcile("snapshot", BrokerSnapshot("paper:synthetic-reconciliation", at,
            Decimal("9900.57"), (BrokerPosition("SPY", 1),), ("orb-exit-demo-1",), True))
        final = ledger.snapshot()
    return {"synthetic": True, "before_restart_reconciled": before["broker_reconciled"],
            "reconciliation": result, **final}
