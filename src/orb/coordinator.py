"""Durable offline admission/account coordination, not a broker dispatcher.

Replay validated commands through the existing account and loss models. The
command, decision and intent effect commit together in one local transaction.
Only synthetic full-fill accounting is supported; external/partial evidence
gates further admissions instead of being forced into this simpler model.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path

from orb.entry_capacity import EntryCaps, capacity_usage, validate_caps
from orb.account import AccountLedger
from orb.daily_loss import DailyLossGuard
from orb.domain import BreakoutSignal
from orb.journal import EntryIntent, IntentJournal, JournalError, _stamp, _token
from orb.risk import AccountSnapshot, MarketMark, RiskLimits, size_long
from orb.session import exchange_session


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _stamp(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _json(value):
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class CoordinatorConfig:
    initial_cash: Decimal = Decimal("10000")
    limits: RiskLimits = RiskLimits()
    daily_loss_fraction: Decimal = Decimal("0.02")
    slippage_bps: Decimal = Decimal("0")
    fee_per_share: Decimal = Decimal("0")
    entry_caps: EntryCaps | None = None

    def __post_init__(self):
        validate_caps(self.entry_caps)
        if not isinstance(self.limits, RiskLimits):
            raise JournalError("coordinator limits must be validated RiskLimits")
        with localcontext(Context(prec=28)):
            size_long(AccountSnapshot(self.initial_cash, self.initial_cash), Decimal("100"), Decimal("99"),
                      self.limits, slippage_bps=self.slippage_bps, fee_per_share=self.fee_per_share)
        if (not isinstance(self.daily_loss_fraction, Decimal) or not self.daily_loss_fraction.is_finite()
                or not 0 < self.daily_loss_fraction <= 1):
            raise JournalError("daily loss fraction must be a finite positive Decimal at most 1")


def _model_id():
    # A changed executable model must not silently reinterpret historical state.
    from importlib.metadata import version
    root = Path(__file__).parent
    names = ("account.py", "risk.py", "daily_loss.py", "session.py", "breakout.py",
             "domain.py", "journal.py", "coordinator.py", "entry_capacity.py")
    h = sha256()
    for name in names:
        h.update(name.encode() + b"\0" + (root / name).read_bytes())
    for package in ("exchange-calendars", "numpy", "pandas", "tzdata", "pyluach", "korean-lunar-calendar"):
        h.update((package + "=" + version(package)).encode())
    return h.hexdigest()


class _State:
    def __init__(self, config):
        self.account = AccountLedger(config.initial_cash, config.limits)
        self.guard = None
        self.marks = {}
        self.at = self.tick_at = None
        self.attempts = {}
        self.filled_sessions = {}


def _marks(evidence, at):
    marks = {}
    for symbol, item in evidence.items():
        if symbol not in ("SPY", "QQQ"):
            raise JournalError("unsupported held-symbol mark")
        mark = MarketMark(Decimal(item["price"]), datetime.fromisoformat(item["available_at"]), item["source"])
        if mark.available_at > at:
            raise JournalError("future marks cannot inform admission")
        marks[symbol] = mark
    return marks


def _equity(state, at):
    try:
        return state.account.marked_snapshot(at, state.marks).equity
    except ValueError:
        return None


def _signal(payload):
    return BreakoutSignal(payload["signal_id"], payload["strategy_version"], payload["symbol"],
                          datetime.fromisoformat(payload["bar_start"]), datetime.fromisoformat(payload["observed_at"]),
                          *(Decimal(payload[n]) for n in ("entry_reference", "stop", "target")))


class DurableCoordinator:
    """One authoritative local file; open one connection per worker.

    Event replay is deliberately simple and O(history) per command. Model and
    configuration identities are fixed. No live submission permission, broker
    reconciliation, partial-fill account model or detector-bar checkpoint exists.
    """

    def __init__(self, path: Path, account_scope: str, config: CoordinatorConfig | None = None, *, create=True):
        self.config = config or CoordinatorConfig()
        config_values = asdict(self.config)
        if self.config.entry_caps is None:
            config_values.pop("entry_caps")  # Preserve the uncapped configuration identity.
        self.config_json = _json(config_values)
        self.config_id = sha256(self.config_json.encode()).hexdigest()
        self.model_id = _model_id()
        self.journal = IntentJournal(path, account_scope, create=create)
        self._db = self.journal._db
        try:
            with self.journal._transaction():
                version = self._db.execute("SELECT version FROM metadata").fetchone()[0]
                if version == 1:
                    if not create or self._db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]:
                        raise JournalError("cannot adopt an existing intent history without account evidence")
                    self._db.execute("CREATE TABLE coordinator_config (config TEXT NOT NULL, model_id TEXT NOT NULL)")
                    self._db.execute("INSERT INTO coordinator_config VALUES (?,?)", (self.config_json, self.model_id))
                    self._db.execute("""CREATE TABLE coordinator_events (
                        sequence INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                        command TEXT NOT NULL, outcome TEXT NOT NULL)""")
                    self._db.execute("UPDATE metadata SET version=2")
                rows = self._db.execute("SELECT * FROM coordinator_config").fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (self.config_json, self.model_id):
                    raise JournalError("coordinator configuration or executable model changed; explicit migration required")
                self._restore()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _restore(self):
        with localcontext(Context(prec=28)):
            state = _State(self.config)
            for row in self._db.execute("SELECT * FROM coordinator_events ORDER BY sequence"):
                result = self._apply(state, json.loads(row["command"]))
                if _json(result) != row["outcome"]:
                    raise JournalError("saved decision disagrees with deterministic model replay")
            return state

    def _gate(self, state):
        rows = {r["signal_id"]: r for r in self._db.execute("SELECT * FROM intents")}
        if set(rows) != {a.order_id for a in state.account.allocations}:
            return True
        for a in state.account.allocations:
            row = rows[a.order_id]
            expected = {"reserved": "prepared", "cancelled": "withdrawn", "open": "filled",
                        "unresolved": "filled", "closed": "filled"}[a.state]
            if row["state"] != expected:
                return True
            if a.entry_price is not None and (row["filled_quantity"] != a.quantity
                    or Decimal(row["filled_notional"]) != a.entry_price * a.quantity):
                return True
            if json.loads(row["payload"])["quantity"] != a.quantity:
                return True
        return False

    def _apply(self, state, command):
        op, at = command["op"], datetime.fromisoformat(command["at"])
        _stamp(at)
        if at.second or at.microsecond:
            raise JournalError("coordinator observations must align to completed-minute availability")
        if state.at is not None and at < state.at:
            raise JournalError("coordinator commands must be chronological")
        if op == "session":
            schedule = exchange_session(at.date())
            if schedule is None or at != schedule.opens_at or (state.guard and schedule.day <= state.guard.session.day):
                raise JournalError("start one actual exchange session at its fixed open")
            state.marks = _marks(command["marks"], at)
            state.guard = DailyLossGuard(schedule, self.config.daily_loss_fraction, _equity(state, at))
            state.at = state.tick_at = at
            return state.guard.observe(at, _equity(state, at))
        if state.guard is None or not state.guard.session.opens_at <= at <= state.guard.session.closes_at:
            raise JournalError("command needs its current exchange session")
        if op == "tick":
            if at <= state.tick_at:
                raise JournalError("a minute observation is already consumed; retry its original request ID")
            # Do not heal an unobserved held-equity gap with a favourable later mark.
            held = any(a.state in ("open", "unresolved") for a in state.account.allocations)
            if held and at - state.tick_at > timedelta(minutes=1):
                state.guard.observe(state.tick_at + timedelta(minutes=1), None)
            state.marks = _marks(command["marks"], at)
            state.at = state.tick_at = at
            return state.guard.observe(at, _equity(state, at))
        if at != state.tick_at:
            raise JournalError("advance the minute clock and marks before account decisions")
        state.at = at
        if op == "candidate":
            signal = _signal(command["signal"])
            base = EntryIntent.from_signal(signal, 1, self.config_id)
            if base.session != str(state.guard.session.day) or at < signal.observed_at:
                raise JournalError("candidate/session observation mismatch")
            key = (base.session, base.symbol)
            if key in state.attempts:
                old_signal, outcome = state.attempts[key]
                if old_signal != command["signal"]:
                    raise JournalError("session candidate already consumed with different evidence")
                return outcome
            before = {"config_id": self.config_id, "model_id": self.model_id, "account": state.account.to_dict(),
                      "loss": state.guard.to_dict(), "at": at, "marks": {k: asdict(v) for k,v in state.marks.items()}}
            context_id = sha256(_json(before).encode()).hexdigest()
            reason = None
            if at >= state.guard.session.entry_cutoff or at - signal.bar_start > timedelta(seconds=150):
                reason = "stale_candidate"
            elif command["reconciliation_blocked"]:
                reason = "reconciliation_required"
            elif state.guard.block_reason:
                reason = state.guard.block_reason
            elif any(a.symbol == signal.symbol and a.state in ("reserved", "open", "unresolved")
                     for a in state.account.allocations):
                reason = "active_symbol"
            capacity = (capacity_usage(state.account.allocations, state.filled_sessions,
                                       state.guard.session.day, self.config.entry_caps)
                        if self.config.entry_caps else None)
            if reason is None and capacity:
                reason = capacity["reason"]
            if reason is None:
                decision = state.account.reserve(signal.signal_id, signal.symbol, signal.entry_reference, signal.stop,
                    slippage_bps=self.config.slippage_bps, fee_per_share=self.config.fee_per_share,
                    at=at, marks=state.marks)
                reason = decision.reason
                outcome = {"quantity": decision.quantity, "reason": reason, "decision": decision.to_dict(),
                           "intent": (asdict(EntryIntent.from_signal(signal, decision.quantity, context_id))
                                      if decision.quantity else None)}
            else:
                outcome = {"quantity": 0, "reason": reason, "intent": None, "decision": None}
            if capacity is not None:
                outcome["entry_capacity"] = capacity
            state.attempts[key] = (command["signal"], outcome)
            return outcome
        order_id = command["signal_id"]
        if op == "cancel":
            state.account.cancel(order_id)
        elif op == "fill":
            price = Decimal(command["price"])
            state.account.fill(order_id, price)
            state.filled_sessions[order_id] = state.guard.session.day
            allocation = next(a for a in state.account.allocations if a.order_id == order_id)
            state.marks[allocation.symbol] = MarketMark(price, at, "entry_fill")
        elif op == "close":
            state.account.close(order_id, Decimal(command["price"]))
        elif op == "unresolved":
            state.account.mark_unresolved(order_id)
        else:
            raise JournalError("unsupported coordinator command")
        return {"account": state.account.to_dict(), "daily_loss": state.guard.observe(at, _equity(state, at))}

    def _effects(self, state, command, outcome):
        op, at = command["op"], datetime.fromisoformat(command["at"])
        if op == "candidate" and outcome["intent"]:
            self.journal.prepare(EntryIntent(**outcome["intent"]), at)
        elif op == "cancel":
            row = self.journal.get(command["signal_id"])
            self.journal.withdraw(row["signal_id"], expected_revision=row["revision"], at=at)
        elif op == "fill":
            row = self.journal.get(command["signal_id"])
            self.journal.claim(row["signal_id"], expected_revision=row["revision"], at=at)
            a = next(a for a in state.account.allocations if a.order_id == row["signal_id"])
            self.journal.observe(row["signal_id"], event_id="research-fill:" + row["signal_id"], state="filled",
                filled_quantity=a.quantity, filled_notional=a.entry_price * a.quantity,
                broker_order_id="research:" + row["signal_id"], expected_revision=row["revision"] + 1, at=at)

    def _execute(self, request_id, command):
        _token(request_id)
        with localcontext(Context(prec=28)), self.journal._transaction():
            old = self._db.execute("SELECT command,outcome FROM coordinator_events WHERE request_id=?", (request_id,)).fetchone()
            if old:
                saved = json.loads(old["command"])
                saved.pop("reconciliation_blocked", None)
                if _json(saved) != _json(command):
                    raise JournalError("conflicting duplicate coordinator request")
                return json.loads(old["outcome"])
            state = self._restore()
            gated = self._gate(state)
            if command["op"] == "candidate":
                command = {**command, "reconciliation_blocked": gated}
            elif gated and command["op"] in ("fill", "cancel", "close", "unresolved"):
                raise JournalError("external journal state requires reconciliation; account resources retained")
            outcome = _plain(self._apply(state, command))
            self._effects(state, command, outcome)
            self._db.execute("INSERT INTO coordinator_events(request_id,command,outcome) VALUES (?,?,?)",
                             (request_id, _json(command), _json(outcome)))
            return outcome

    @staticmethod
    def _mark_evidence(marks):
        return {k: _plain(asdict(v)) for k,v in (marks or {}).items()}

    def start_session(self, request_id, day: date, marks=None):
        session = exchange_session(day)
        if session is None:
            raise JournalError("date is not an exchange session")
        return self._execute(request_id, {"op": "session", "at": _stamp(session.opens_at), "marks": self._mark_evidence(marks)})

    def tick(self, request_id, at: datetime, marks=None):
        return self._execute(request_id, {"op": "tick", "at": _stamp(at), "marks": self._mark_evidence(marks)})

    def admit(self, request_id, signal: BreakoutSignal, *, at=None):
        validated = EntryIntent.from_signal(signal, 1, self.config_id)
        evidence = asdict(validated)
        for name in ("quantity", "context_id", "session"):
            evidence.pop(name)
        return self._execute(request_id, {"op": "candidate", "at": _stamp(at or signal.observed_at), "signal": evidence})

    def account_event(self, request_id, operation, signal_id, at: datetime, *, price=None):
        if operation not in ("fill", "cancel", "close", "unresolved"):
            raise JournalError("supported modeled account operations: fill/cancel/close/unresolved")
        command = {"op": operation, "at": _stamp(at), "signal_id": signal_id}
        if operation in ("fill", "close"):
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise JournalError("modeled fill/exit price must be a positive finite Decimal")
            command["price"] = str(price)
        elif price is not None:
            raise JournalError("this account operation does not take a price")
        return self._execute(request_id, command)

    def snapshot(self):
        with localcontext(Context(prec=28)), self.journal._transaction():
            state = self._restore()
            return _plain({"model": ("durable-offline-entry-caps-v1" if self.config.entry_caps else "durable-offline-coordinator-v1"), "account_scope": self.journal.account_scope,
                "config_id": self.config_id, "account": state.account.to_dict(),
                "model_id": self.model_id,
                "daily_loss": state.guard.to_dict() if state.guard else None, "at": state.at,
                "attempts": [{"session": k[0], "symbol": k[1], "outcome": v[1]} for k,v in sorted(state.attempts.items())],
                "reconciliation_required": self._gate(state), "broker_reconciled": False,
                "paper_execution_enabled": False, "modeled_account_restored": True,
                **({"entry_capacity": capacity_usage(state.account.allocations, state.filled_sessions,
                    state.guard.session.day, self.config.entry_caps)} if self.config.entry_caps and state.guard else {}),
                "events": self._db.execute("SELECT COUNT(*) FROM coordinator_events").fetchone()[0]})

    def recover(self, at: datetime):
        # Lifecycle-only recovery; never reconstruct external holdings from intent totals.
        with self.journal._transaction():
            state = self._restore()
            _stamp(at)
            if state.at is not None and at < state.at:
                raise JournalError("recovery cannot precede the recorded account clock")
            self.journal.recover(at)
            return self.snapshot()


def coordinator_demo(path: Path):
    """Invented fill/loss/restart exercise, not provider data or performance."""
    with Path(path).open("xb"):
        pass
    config = CoordinatorConfig(daily_loss_fraction=Decimal("0.001"), fee_per_share=Decimal("0.01"))
    scope = "paper:synthetic-coordinator"
    at = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)

    def candidate(symbol, observed):
        identity = f"orb-long-v1|{symbol}|2026-10-09|1"
        return BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], "orb-long-v1", symbol,
            observed - timedelta(minutes=1), observed, Decimal("100.20"), Decimal("99"), Decimal("102.60"))

    spy = candidate("SPY", at)
    later = at + timedelta(minutes=1)
    with DurableCoordinator(path, scope, config) as c:
        c.start_session("session", at.date())
        c.tick("minute", at)
        c.admit("spy", spy)
        c.account_event("fill", "fill", spy.signal_id, at, price=Decimal("100.20"))
        c.tick("loss", later, {"SPY": MarketMark(Decimal("99"), later)})
        c.account_event("exit", "close", spy.signal_id, later, price=Decimal("99"))
        before = c.snapshot()
    with DurableCoordinator(path, scope, config, create=False) as c:
        restored = c.snapshot() == before
        c.admit("qqq", candidate("QQQ", later))
        return {"synthetic": True, "orders_submitted": False,
                "restored_identically": restored, "state": c.snapshot()}
