"""Bounded, read-only Alpaca paper-account snapshot normalisation.

Only fixed GET requests are available here.  Two consecutive account/position/
open-order passes must normalise identically before the capture is declared
complete.  This is a conservative quiescence check, not an atomic broker
transaction and never an order-submission capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from orb.acquisition import Credentials
from orb.ingestion import _decode
from orb.journal import JournalError, _token
from orb.reconciliation import BrokerPosition, BrokerSnapshot, ReconciliationError


BASE_URL = "https://paper-api.alpaca.markets"
SNAPSHOT_SCHEMA = "orb-alpaca-paper-snapshot-v1"
RESOURCES = ("account", "positions", "orders")


class PaperSnapshotError(ValueError):
    """Sanitised adapter failure; response bodies and credentials stay private."""


@dataclass(frozen=True)
class SnapshotLimits:
    timeout_seconds: int = 10
    max_response_bytes: int = 1_000_000
    max_total_bytes: int = 6_000_000
    order_limit: int = 500

    def __post_init__(self):
        bounds = {"timeout_seconds": 30, "max_response_bytes": 2_000_000,
                  "max_total_bytes": 12_000_000, "order_limit": 500}
        if any(type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum
               for name, maximum in bounds.items()):
            raise PaperSnapshotError("snapshot limits must be positive integers within hard safety bounds")
        if self.max_total_bytes < self.max_response_bytes:
            raise PaperSnapshotError("total byte limit must cover one response")


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class _Account:
    account_scope: str
    cash: Decimal
    active: bool
    unblocked: bool


@dataclass(frozen=True)
class _View:
    account: _Account
    positions: tuple[BrokerPosition, ...]
    open_client_order_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PaperSnapshotCapture:
    observed_from: datetime
    observed_to: datetime
    snapshot: BrokerSnapshot | None
    reasons: tuple[str, ...]
    response_sha256: tuple[tuple[str, str, str], ...]

    @property
    def complete(self) -> bool:
        return self.snapshot is not None and self.snapshot.complete

    def payload(self) -> dict:
        return {
            "schema_version": SNAPSHOT_SCHEMA,
            "provider": "alpaca",
            "environment": "paper",
            "method": "GET",
            "read_only": True,
            "requests_made": len(self.response_sha256),
            "observed_from": self.observed_from.astimezone(timezone.utc).isoformat(),
            "observed_to": self.observed_to.astimezone(timezone.utc).isoformat(),
            "complete": self.complete,
            "reasons": list(self.reasons),
            "broker_snapshot": None if self.snapshot is None else self.snapshot.payload(),
            "response_sha256": [
                {"pass": pass_name, "resource": resource, "sha256": digest}
                for pass_name, resource, digest in self.response_sha256
            ],
            "paper_execution_enabled": False,
            "orders_submitted": False,
            "consistency_note": (
                "two equal normalised passes show bounded quiescence, not an atomic provider snapshot"
            ),
        }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(url: str, headers: dict[str, str], timeout: int, max_bytes: int) -> HTTPResponse:
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            chunks, count = [], 0
            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise PaperSnapshotError("paper response reading exceeded the time bound")
                chunk = response.read1(min(65536, max_bytes + 1 - count))
                count += len(chunk)
                if count > max_bytes:
                    raise PaperSnapshotError("paper response exceeds byte limit")
                if not chunk:
                    break
                chunks.append(chunk)
            return HTTPResponse(response.status, b"".join(chunks))
    except HTTPError as exc:
        status = exc.code
        exc.close()
        return HTTPResponse(status)
    except (URLError, OSError, TimeoutError):
        raise PaperSnapshotError("paper-account transport failed") from None


def snapshot_plan(limits: SnapshotLimits | None = None) -> dict:
    limits = limits or SnapshotLimits()
    query = urlencode({"status": "open", "limit": limits.order_limit,
                       "direction": "asc", "nested": "false"})
    return {
        "base_url": BASE_URL,
        "method": "GET",
        "redirects": "rejected",
        "passes": 2,
        "maximum_http_requests": 6,
        "resources": ["/v2/account", "/v2/positions", "/v2/orders?" + query],
        "limits": asdict(limits),
        "credentials": "environment only",
        "paper_execution_enabled": False,
        "orders_submitted": False,
    }


def _decimal_string(value, name: str, *, allow_negative: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise PaperSnapshotError(f"invalid paper {name}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise PaperSnapshotError(f"invalid paper {name}") from None
    if not number.is_finite() or (number < 0 and not allow_negative):
        raise PaperSnapshotError(f"invalid paper {name}")
    return number


def _account(value) -> _Account:
    if not isinstance(value, dict):
        raise PaperSnapshotError("invalid paper account response")
    try:
        account_id, cash, status = value["id"], value["cash"], value["status"]
        blocked = (value["account_blocked"], value["trading_blocked"],
                   value["trade_suspended_by_user"])
    except KeyError:
        raise PaperSnapshotError("incomplete paper account response") from None
    if not isinstance(account_id, str) or len(account_id) > 64:
        raise PaperSnapshotError("invalid paper account identity")
    try:
        canonical_id = str(UUID(account_id))
    except (ValueError, AttributeError):
        raise PaperSnapshotError("invalid paper account identity") from None
    if account_id.lower() != canonical_id:
        raise PaperSnapshotError("paper account identity must be canonical UUID")
    if not isinstance(status, str) or any(type(flag) is not bool for flag in blocked):
        raise PaperSnapshotError("invalid paper account status")
    scope = "paper:alpaca:" + sha256(account_id.encode()).hexdigest()[:24]
    return _Account(scope, _decimal_string(cash, "cash"), status == "ACTIVE", not any(blocked))


def _positions(value) -> tuple[tuple[BrokerPosition, ...], tuple[str, ...]]:
    if not isinstance(value, list):
        raise PaperSnapshotError("invalid paper positions response")
    positions, reasons, seen = [], set(), set()
    for item in value:
        if not isinstance(item, dict):
            raise PaperSnapshotError("invalid paper position")
        try:
            symbol, quantity, side, asset_class = (
                item["symbol"], item["qty"], item["side"], item["asset_class"]
            )
        except KeyError:
            raise PaperSnapshotError("incomplete paper position") from None
        if not isinstance(symbol, str) or symbol in seen:
            raise PaperSnapshotError("invalid or duplicate paper position symbol")
        seen.add(symbol)
        if symbol not in ("SPY", "QQQ") or side != "long" or asset_class != "us_equity":
            reasons.add("unsupported_position")
            continue
        number = _decimal_string(quantity, "position quantity", allow_negative=True)
        if number <= 0 or number != number.to_integral_value() or number > 2_147_483_647:
            reasons.add("unsupported_position_quantity")
            continue
        positions.append(BrokerPosition(symbol, int(number)))
    return tuple(sorted(positions, key=lambda item: item.symbol)), tuple(sorted(reasons))


def _orders(value, limit: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value, list):
        raise PaperSnapshotError("invalid paper orders response")
    reasons = set()
    if len(value) >= limit:
        reasons.add("open_order_limit_reached")
    identities = []
    for item in value:
        if not isinstance(item, dict) or "client_order_id" not in item:
            raise PaperSnapshotError("incomplete paper order")
        identity = item["client_order_id"]
        try:
            _token(identity)
        except JournalError:
            raise PaperSnapshotError("invalid paper client order identity") from None
        if identity in identities:
            raise PaperSnapshotError("duplicate paper client order identity")
        identities.append(identity)
    return tuple(sorted(identities)), tuple(sorted(reasons))


def _normalise(responses: dict[str, object], limit: int) -> _View:
    account = _account(responses["account"])
    positions, position_reasons = _positions(responses["positions"])
    orders, order_reasons = _orders(responses["orders"], limit)
    reasons = set(position_reasons + order_reasons)
    if not account.active:
        reasons.add("account_not_active")
    if not account.unblocked:
        reasons.add("account_blocked")
    return _View(account, positions, orders, tuple(sorted(reasons)))


def _read_pass(pass_name: str, credentials: Credentials, limits: SnapshotLimits,
               transport, digests: list[tuple[str, str, str]], total: list[int]) -> _View:
    plan = snapshot_plan(limits)
    headers = {"APCA-API-KEY-ID": credentials.key, "APCA-API-SECRET-KEY": credentials.secret,
               "Accept": "application/json", "User-Agent": "orb-paper-snapshot/1"}
    decoded = {}
    for resource, path in zip(RESOURCES, plan["resources"]):
        url = BASE_URL + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "paper-api.alpaca.markets":
            raise PaperSnapshotError("paper endpoint policy violation")
        response = transport(url, headers, limits.timeout_seconds, limits.max_response_bytes)
        if type(response) is not HTTPResponse:
            raise PaperSnapshotError("invalid paper transport response")
        if response.status in (401, 403):
            raise PaperSnapshotError("paper authentication denied")
        if response.status != 200:
            raise PaperSnapshotError(f"paper HTTP status {response.status}; request stopped")
        if len(response.body) > limits.max_response_bytes:
            raise PaperSnapshotError("paper response exceeds byte limit")
        total[0] += len(response.body)
        if total[0] > limits.max_total_bytes:
            raise PaperSnapshotError("paper responses exceed total byte limit")
        if credentials.key.encode() in response.body or credentials.secret.encode() in response.body:
            raise PaperSnapshotError("paper response contains credential material")
        try:
            decoded[resource] = _decode(response.body)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise PaperSnapshotError(f"invalid paper {resource} JSON") from None
        digests.append((pass_name, resource, sha256(response.body).hexdigest()))
    return _normalise(decoded, limits.order_limit)


def capture_paper_snapshot(*, credentials: Credentials | None = None,
                           limits: SnapshotLimits | None = None, transport=None,
                           clock=None) -> PaperSnapshotCapture:
    """Capture six bounded GET responses and return redacted normalised evidence."""
    limits = limits or SnapshotLimits()
    credentials = credentials or Credentials.from_environment()
    transport = transport or _http_get
    clock = clock or (lambda: datetime.now(timezone.utc))
    observed_from = clock()
    if (not isinstance(observed_from, datetime) or observed_from.tzinfo is None
            or observed_from.utcoffset() is None):
        raise PaperSnapshotError("snapshot clock must be timezone-aware")
    digests: list[tuple[str, str, str]] = []
    total = [0]
    first = _read_pass("first", credentials, limits, transport, digests, total)
    second = _read_pass("second", credentials, limits, transport, digests, total)
    observed_to = clock()
    if (not isinstance(observed_to, datetime) or observed_to.tzinfo is None
            or observed_to.utcoffset() is None or observed_to < observed_from):
        raise PaperSnapshotError("snapshot clock moved backwards or became naive")
    reasons = set(first.reasons + second.reasons)
    if first.account != second.account:
        reasons.add("account_changed_between_passes")
    if first.positions != second.positions:
        reasons.add("positions_changed_between_passes")
    if first.open_client_order_ids != second.open_client_order_ids:
        reasons.add("orders_changed_between_passes")
    unsupported = bool(reasons & {"unsupported_position", "unsupported_position_quantity"})
    snapshot = None
    if not unsupported:
        try:
            snapshot = BrokerSnapshot(
                second.account.account_scope, observed_to.astimezone(timezone.utc), second.account.cash,
                second.positions, second.open_client_order_ids, not reasons
            )
            snapshot.payload()
        except (ReconciliationError, JournalError):
            raise PaperSnapshotError("normalised snapshot violates reconciliation contract") from None
    return PaperSnapshotCapture(observed_from, observed_to, snapshot,
                                tuple(sorted(reasons)), tuple(digests))
