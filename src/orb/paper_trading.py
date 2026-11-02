"""Single-account SPY paper worker; fixed paper endpoint, durable one-attempt/day.

Broker state is authoritative. Intent commits before POST; uncertain POSTs are
looked up, never resent. A file lock excludes competing conforming workers.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, build_opener

from orb.acquisition import Credentials, _NoRedirect
from orb.breakout import BreakoutDetector
from orb.domain import Bar
from orb.ingestion import _decode
from orb.research_settings import ResearchSettings
from orb.risk import AccountSnapshot, size_long
from orb.session import exchange_session
from orb.strategy import calculate_opening_range, NEW_YORK

BROKER = 'https://paper-api.alpaca.markets'
DATA = 'https://data.alpaca.markets'
TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}


class PaperError(ValueError):
    pass


def decimal(v):
    if not isinstance(v, (str, int, float, Decimal)) or isinstance(v, bool):
        raise PaperError('Missing broker numeric evidence.')
    d = Decimal(str(v))
    if not d.is_finite() or abs(d) > Decimal('1e12'):
        raise PaperError('Invalid broker numeric evidence.')
    return d


def timestamp(v):
    if not isinstance(v, str): raise PaperError('Missing broker timestamp.')
    d = datetime.fromisoformat(v.replace('Z', '+00:00'))
    if d.tzinfo is None: raise PaperError('Broker timestamp needs a timezone.')
    return d.astimezone(timezone.utc)


def _transport(method, url, headers, payload):
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                  headers=headers, method=method)
    try:
        with build_opener(_NoRedirect()).open(req, timeout=8) as r:
            body = r.read(2_000_001)
            if len(body) > 2_000_000: raise PaperError('Broker response exceeds limit.')
            return r.status, body
    except HTTPError as exc:
        return exc.code, b''
    except (URLError, TimeoutError, OSError):
        raise PaperError('Paper network failed; uncertain orders will not be resent.') from None


class PaperClient:
    def __init__(self, credentials=None, transport=None):
        self.credentials = credentials or Credentials.from_environment()
        self.transport = transport or _transport

    def request(self, method, path, payload=None, *, data=False, missing=False):
        if not path.startswith('/v2/') or '..' in path: raise PaperError('Invalid paper resource.')
        if data and method != 'GET': raise PaperError('Market data is read-only.')
        headers = {'APCA-API-KEY-ID': self.credentials.key, 'APCA-API-SECRET-KEY': self.credentials.secret,
                   'Content-Type': 'application/json', 'Accept': 'application/json'}
        status, body = self.transport(method, (DATA if data else BROKER) + path, headers, payload)
        if status == 404 and missing: return None
        if status not in (200, 201, 204): raise PaperError(f'Paper HTTP {status}; inspect connection/orders.')
        if not body: return None
        if len(body) > 2_000_000 or any(v.encode() in body for v in (self.credentials.key, self.credentials.secret)):
            raise PaperError('Invalid private broker response.')
        try: return _decode(body)
        except (ValueError, UnicodeError, RecursionError): raise PaperError('Invalid paper JSON.') from None

    def find(self, client_id):
        return self.request('GET', '/v2/orders:by_client_order_id?' + urlencode(
            {'client_order_id': client_id}), missing=True)

    def order(self, identity):
        return self.request('GET', '/v2/orders/' + quote(identity, safe='') + '?nested=true')

    def bars(self, session, now, feed):
        p = dict(timeframe='1Min', start=session.opens_at.isoformat(),
                 end=(now - timedelta(minutes=1)).isoformat(), limit=1000, feed=feed, adjustment='raw', sort='asc')
        response = self.request('GET', '/v2/stocks/SPY/bars?' + urlencode(p), data=True)
        if not isinstance(response, dict) or response.get('next_page_token'):
            raise PaperError('Incomplete market data page.')
        bars = [Bar('SPY', timestamp(b['t']), *(decimal(b[k]) for k in ('o', 'h', 'l', 'c')), b['v'])
                for b in response['bars'] or []]
        if any(a.start >= b.start for a, b in zip(bars, bars[1:])):
            raise PaperError('Duplicate or out-of-order market minutes.')
        return [b for b in bars if session.contains_bar(b.start) and b.start + timedelta(minutes=1) <= now]


class PaperEngine:
    """SPY long, one entry/day, flat account on entry, no backfilled orders.

    GTC brackets protect full fills at broker. At flatten deadline cancel owned
    bracket, verify terminal legs, then sell only reconciled residual quantity.
    Shutdown leaves broker exits intact. Restart requires explicit Start.
    """
    def __init__(self, path, client=None):
        self.path = Path(path)
        if self.path.is_symlink(): raise PaperError("Paper state symlinks are not supported.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.lock = RLock()
        self.file_lock = self.thread = None
        self.quit = Event()
        self.enabled = False
        self.message = 'Stopped. Choose a paper preset and start.'
        self.market = self.updated_at = self.record = None
        self.feed = 'iex'
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('BEGIN IMMEDIATE')
            identity = self.db.execute('PRAGMA application_id').fetchone()[0]
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if identity == version == 0 and not tables:
                self.db.execute('CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE paper_attempts (day TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL, '
                                'request TEXT NOT NULL, preset TEXT NOT NULL, closing TEXT, state TEXT NOT NULL)')
                self.db.execute('PRAGMA application_id=1330795600')
                self.db.execute('PRAGMA user_version=1')
            elif identity != 1330795600 or version != 1 or tables != {'paper_meta', 'paper_attempts'}:
                raise PaperError('Unrecognised paper database; no changes made.')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.close()
            raise

    def meta(self, key, value=None):
        if value is not None: self.db.execute('INSERT OR REPLACE INTO paper_meta VALUES (?,?)', (key, value))
        r = self.db.execute('SELECT value FROM paper_meta WHERE key=?', (key,)).fetchone()
        return r[0] if r else None

    def status(self):
        with self.lock:
            return dict(enabled=self.enabled, environment='paper', symbol='SPY', feed=self.feed,
                        message=self.message, updated_at=self.updated_at, preset=self.record, market=self.market,
                        attempts=[dict(r) | {'request': json.loads(r['request']), 'preset': json.loads(r['preset'])}
                                  for r in self.db.execute('SELECT * FROM paper_attempts ORDER BY day DESC LIMIT 20')])

    @staticmethod
    def validate(record, feed):
        s = ResearchSettings.from_dict(record['settings'])
        if (feed not in ('iex', 'sip') or s.initial_cash > 100000 or s.risk_fraction > Decimal('.01')
                or s.daily_loss_fraction > Decimal('.03') or s.max_session_entries != 1
                or s.max_concurrent_positions != 1 or s.target_r > 3):
            raise PaperError('Paper v1: allocation ≤ $100,000, risk ≤ 1%, daily loss ≤ 3%, '
                             'one entry/position and target ≤ 3R. Duplicate a paper starter preset.')
        return s

    def start(self, record, feed='iex'):
        settings = self.validate(record, feed)
        with self.lock:
            if self.enabled: raise PaperError('Paper worker already running; stop before changing settings.')
            if self.thread and self.thread.is_alive():
                # Existing management thread can rearm without spawning a competitor.
                self.settings, self.record, self.feed = settings, json.loads(json.dumps(record)), feed
                self.enabled = True
                return self.status()
            lock_path = self.path.with_suffix('.lock')
            if lock_path.is_symlink(): raise PaperError('Paper lock symlinks are not supported.')
            fd = lock_path.open('a')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fd.close()
                raise PaperError('Another paper worker owns this state.') from None
            try:
                self.client = self.client or PaperClient()
                self.settings, self.record, self.feed = settings, json.loads(json.dumps(record)), feed
                self.file_lock = fd
                self.enabled = True
                self.quit.clear()
                self.message = 'Starting paper worker…'
                self.thread = Thread(target=self._loop, name='orb-paper', daemon=True)
                self.thread.start()
            except BaseException:
                fd.close()
                self.enabled = False
                raise
        return self.status()

    def stop(self):
        with self.lock:
            self.enabled = False
            self.message = 'Entries stopped. Existing ORB orders remain managed while this server runs.'
        return self.status()

    def close(self):
        self.quit.set()
        if self.thread: self.thread.join(timeout=45)
        if self.thread and self.thread.is_alive(): return
        self.db.close()

    def _loop(self):
        try:
            while not self.quit.is_set():
                try:
                    with self.lock: self.tick(datetime.now(timezone.utc))
                except PaperError as exc:
                    with self.lock:
                        self.enabled = False
                        self.message = str(exc)
                except Exception:
                    with self.lock:
                        self.enabled = False
                        self.message = 'Worker paused after a broker/data error. Refresh account and inspect orders; restart to reconcile.'
                if self.quit.wait(15): break
        finally:
            with self.lock:
                self.enabled = False
                if self.file_lock:
                    self.file_lock.close()
                    self.file_lock = None

    def tick(self, now):
        started = monotonic()
        self.updated_at = now.isoformat()
        clock = self.client.request('GET', '/v2/clock')
        if abs(now - timestamp(clock['timestamp'])) > timedelta(seconds=30): raise PaperError('Broker clock is stale.')
        if type(clock.get('is_open')) is not bool: raise PaperError('Missing market-open evidence.')
        account = self.client.request('GET', '/v2/account')
        scope = sha256(account['id'].encode()).hexdigest()
        saved = self.meta('account')
        if saved and saved != scope: raise PaperError('Database belongs to a different paper account.')
        self.meta('account', scope)
        today = now.astimezone(NEW_YORK).date()
        session = exchange_session(today)
        for row in list(self.db.execute("SELECT * FROM paper_attempts WHERE state != 'done'")):
            if not self.manage(row, now, clock['is_open']): return
        if not self.enabled: return
        if not session or not clock['is_open']:
            self.message = 'Waiting for the next regular US session. No orders sent.'
            return
        if now < session.range_end:
            self.message = 'Building the 09:30–09:45 New York opening range.'
            self.update_market(session, now)
            return
        if now >= session.entry_cutoff:
            self.message = 'Entry window closed (11:00 New York). Waiting for the next session.'
            self.update_market(session, now)
            return
        if self.db.execute('SELECT 1 FROM paper_attempts WHERE day=?', (str(today),)).fetchone():
            self.message = 'Session attempt used. No further entries today.'
            self.update_market(session, now)
            return
        if today.weekday() not in self.settings.weekdays:
            self.message = 'This weekday is excluded by the active preset.'
            return
        if account.get('status') != 'ACTIVE' or account.get('trading_blocked') is not False or account.get('account_blocked') is not False:
            raise PaperError('Account is not enabled for paper trading.')
        positions = self.client.request('GET', '/v2/positions')
        orders = self.client.request('GET', '/v2/orders?status=open&limit=500&nested=false')
        if not isinstance(positions, list) or not isinstance(orders, list): raise PaperError('Invalid account inventory.')
        if positions or orders:
            self.message = 'Entry blocked: account must be flat with no open broker orders.'
            return
        equity, cash = decimal(account['equity']), decimal(account['cash'])
        allocation = min(self.settings.initial_cash, equity, cash)
        anchor_key = 'equity:' + str(today)
        anchor = decimal(self.meta(anchor_key) or self.meta(anchor_key, str(equity)))
        if equity <= anchor - min(anchor, self.settings.initial_cash) * self.settings.daily_loss_fraction:
            self.message = 'Daily loss threshold reached. New entries blocked.'
            return
        bars = self.update_market(session, now)
        opening = calculate_opening_range(bars, 'SPY', today, now)
        detector = BreakoutDetector(opening, session, entry_policy=self.settings.engine_kwargs()['entry_policy'])
        signal = None
        for bar in bars:
            if bar.start >= opening.end:
                signal = detector.on_bar(bar, bar.start + timedelta(minutes=1))
                if signal: break
        if not signal and detector.pause_reason:
            raise PaperError('Market minute sequence incomplete; no order submitted.')
        if not signal:
            self.message = 'Waiting for a completed-minute breakout above the opening range.'
            return
        if now - signal.observed_at > timedelta(seconds=90):
            self.message = 'Earlier breakout missed; no retrospective order will be sent.'
            return
        q = self.client.request('GET', '/v2/stocks/SPY/quotes/latest?' + urlencode({'feed': self.feed}), data=True)['quote']
        age = now - timestamp(q['t'])
        if not timedelta(0) <= age <= timedelta(seconds=15): raise PaperError('Quote is stale.')
        ask, bid = decimal(q['ap']), decimal(q['bp'])
        if not 0 < bid <= ask or ask - bid > ask * Decimal('.001'): raise PaperError('Quote spread too wide or invalid.')
        stop = signal.stop.quantize(Decimal('.01'), rounding=ROUND_DOWN)
        entry = (ask * Decimal('1.0005')).quantize(Decimal('.01'), rounding=ROUND_UP)
        if ask <= opening.high or ask <= stop or ask > signal.entry_reference + (signal.entry_reference - stop) / 4:
            self.message = 'Entry skipped: quote moved outside the breakout admission band.'
            return
        asset = self.client.request('GET', '/v2/assets/SPY')
        if asset.get('tradable') is not True: raise PaperError('SPY is not tradable.')
        if allocation <= 0: raise PaperError('No available cash allocation.')
        sized = size_long(AccountSnapshot(allocation, min(cash, allocation)), entry, stop,
                          self.settings.engine_kwargs()['limits'], slippage_bps=self.settings.slippage_bps,
                          fee_per_share=self.settings.fee_per_share)
        if not sized.quantity:
            self.message = 'Entry below one whole share after sizing caps.'
            return
        target = (entry + (entry - stop) * self.settings.target_r).quantize(Decimal('.01'), rounding=ROUND_UP)
        client_id = 'orbp-' + sha256((scope + str(today) + 'SPY').encode()).hexdigest()[:32]
        payload = dict(symbol='SPY', qty=str(sized.quantity), side='buy', type='limit', limit_price=str(entry),
                       time_in_force='gtc', extended_hours=False, order_class='bracket', client_order_id=client_id,
                       take_profit={'limit_price': str(target)}, stop_loss={'stop_price': str(stop)})
        # Provider requests can consume the remainder of a signal/entry window.
        # Elapsed monotonic time keeps fixture clocks deterministic while
        # preventing a slow request from admitting against stale tick-start time.
        decision_at = now + timedelta(seconds=monotonic() - started)
        if (decision_at >= session.entry_cutoff
                or decision_at - timestamp(q['t']) > timedelta(seconds=15)
                or decision_at - signal.observed_at > timedelta(seconds=90)):
            raise PaperError('Entry evidence expired during requests; no order submitted.')
        if self.quit.is_set() or not self.enabled: return
        self.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',
                        (str(today), client_id, json.dumps(payload), json.dumps(self.record), None, 'reserved'))
        self.client.request('POST', '/v2/orders', payload)
        self.message = f'Paper bracket submitted: {sized.quantity} SPY; planned stop risk ${sized.quantity * (entry-stop):.2f}.'

    def update_market(self, session, now):
        bars = ([] if now < session.opens_at + timedelta(minutes=1)
                else self.client.bars(session, now, self.feed))
        opening = []
        try:
            r = calculate_opening_range(bars, 'SPY', session.day, now)
            opening = [dict(symbol='SPY', session=str(session.day), start=r.start.isoformat(),
                            available_at=r.end.isoformat(), high=str(r.high), low=str(r.low))]
        except ValueError: pass
        self.market = dict(evaluation_id='paper:' + str(session.day), session=str(session.day),
                           candles=[dict(symbol=b.symbol, start=b.start.isoformat(), open=str(b.open),
                           high=str(b.high), low=str(b.low), close=str(b.close), volume=b.volume) for b in bars],
                           opening_ranges=opening, decisions=[], import_context={'synthetic': False},
                           feed=self.feed, as_of=now.isoformat())
        return bars

    def manage(self, row, now, is_open):
        parent = self.client.find(row['client_id'])
        if parent is None:
            self.message = 'Uncertain order: intent saved, broker order not found. No automatic resubmission. Inspect Alpaca.'
            return False
        parent = self.client.order(parent['id'])
        expected = json.loads(row['request'])
        if (parent.get('client_order_id') != row['client_id'] or parent.get('symbol') != 'SPY'
                or parent.get('side') != 'buy' or decimal(parent.get('qty')) != decimal(expected['qty'])
                or parent.get('replaced_by') or parent.get('status') == 'replaced'):
            raise PaperError('Owned order identity changed; inspect broker orders.')
        legs = parent.get('legs') or []
        if not isinstance(legs, list) or len(legs) > 2 or any(o.get('symbol') != 'SPY' or o.get('side') != 'sell'
                or o.get('replaced_by') or o.get('status') == 'replaced' for o in legs):
            raise PaperError('Bracket exit identity changed.')
        identities = [o.get('id') for o in [parent, *legs]]
        if (any(not isinstance(i, str) or not i for i in identities)
                or len(set(identities)) != len(identities)):
            raise PaperError('Missing or duplicate bracket order identity.')
        entry_filled = decimal(parent['filled_qty'])
        if not 0 <= entry_filled <= decimal(expected['qty']) or any(decimal(o['filled_qty']) < 0 for o in legs):
            raise PaperError('Invalid cumulative fill quantity.')
        residual = entry_filled - sum((decimal(o['filled_qty']) for o in legs), Decimal(0))
        if residual < 0: raise PaperError('Bracket exits exceed entry fills.')
        # Empty/truncated nested responses are unknown protection, never proof
        # that both exits were canceled. Do not cancel or sell from this state.
        if len(legs) != 2 and (entry_filled > 0 or parent['status'] not in TERMINAL):
            raise PaperError('Incomplete bracket leg evidence; inspect Alpaca protection. No automatic close.')
        close_request = json.loads(row['closing']) if row['closing'] else None
        close_order = self.client.find(close_request['client_order_id']) if close_request else None
        if close_request and close_order is None:
            self.message = 'Uncertain closing order; not resent. Inspect Alpaca.'
            return False
        if close_order:
            if (close_order.get('client_order_id') != close_request['client_order_id']
                    or close_order.get('symbol') != 'SPY' or close_order.get('side') != 'sell'
                    or close_order.get('replaced_by') or close_order.get('status') == 'replaced'
                    or decimal(close_order.get('qty')) != decimal(close_request['qty'])
                    or not 0 <= decimal(close_order['filled_qty']) <= decimal(close_request['qty'])):
                raise PaperError('Closing order identity changed.')
            residual -= decimal(close_order['filled_qty'])
        if residual < 0: raise PaperError('Closing fills exceed entry fills.')
        if close_order and close_order['status'] in TERMINAL and residual > 0:
            raise PaperError('Closing order ended with remaining ORB quantity; inspect Alpaca. No automatic resubmission.')
        terminal = parent['status'] in TERMINAL and all(o['status'] in TERMINAL for o in legs)
        if terminal and residual == 0 and (close_order is None or close_order['status'] in TERMINAL):
            self.db.execute("UPDATE paper_attempts SET state='done' WHERE day=?", (row['day'],))
            self.message = 'Paper attempt complete; broker reports no remaining ORB quantity.'
            return True
        day_session = exchange_session(datetime.fromisoformat(row['day']).date())
        flatten = now >= day_session.flatten_at
        partial = parent['status'] == 'partially_filled'
        stale_entry = entry_filled == 0 and now - timestamp(parent['submitted_at']) > timedelta(seconds=60)
        protection_ended = residual > 0 and any(o['status'] in TERMINAL for o in legs)
        if (flatten or partial or stale_entry or protection_ended) and not terminal and is_open:
            for o in [parent, *legs]:
                if o['status'] not in TERMINAL:
                    self.client.request('DELETE', '/v2/orders/' + quote(o['id'], safe=''))
            self.message = 'Waiting for owned bracket cancellation before flattening.'
            return False
        if terminal and residual > 0 and not close_request and is_open:
            positions = self.client.request('GET', '/v2/positions')
            held = sum((decimal(p['qty']) for p in positions if p['symbol'] == 'SPY'), Decimal(0))
            orders = self.client.request('GET', '/v2/orders?status=open&limit=500&nested=false')
            if held != residual or any(o['symbol'] == 'SPY' for o in orders):
                raise PaperError('Position/order reconciliation failed before flatten.')
            close_request = dict(symbol='SPY', side='sell', qty=str(residual), type='market',
                                 time_in_force='day', client_order_id=row['client_id'] + '-x')
            self.db.execute('UPDATE paper_attempts SET closing=? WHERE day=?', (json.dumps(close_request), row['day']))
            self.client.request('POST', '/v2/orders', close_request)
            self.message = 'Residual ORB position submitted for paper flatten.'
            return False
        self.message = 'Managing existing paper bracket; no new entry.'
        if is_open and day_session.day == now.astimezone(NEW_YORK).date(): self.update_market(day_session, now)
        return False
