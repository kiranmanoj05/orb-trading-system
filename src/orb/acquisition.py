"""Bounded read-only historical GETs to one fixed HTTPS market-data endpoint."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from orb.ingestion import CAPTURE_SCHEMA, ENDPOINT, _decode, _fields, _stamp, write_bundle


class AcquisitionError(ValueError):
    """Sanitised user-facing error: never include headers or response bodies."""


class TransportError(AcquisitionError):
    """Sanitised retryable network failure."""


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self):
        if any(not isinstance(v, str) or not v or len(v) > 256 or
               any(not 33 <= ord(c) <= 126 for c in v) for v in (self.key, self.secret)):
            raise AcquisitionError("credentials must be nonempty visible ASCII without whitespace")

    @classmethod
    def from_environment(cls):
        values = [os.environ.get(n) for n in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")]
        if not all(values):
            raise AcquisitionError("configure APCA_API_KEY_ID and APCA_API_SECRET_KEY securely in the execution environment; never paste keys in chat")
        return cls(*values)


@dataclass(frozen=True)
class FetchLimits:
    max_pages: int = 20
    max_attempts: int = 3
    max_page_bytes: int = 5_000_000
    max_total_bytes: int = 25_000_000
    timeout_seconds: int = 15
    max_retry_delay: int = 10

    def __post_init__(self):
        bounds = {"max_pages": 100, "max_attempts": 5, "max_page_bytes": 10_000_000,
                  "max_total_bytes": 100_000_000, "timeout_seconds": 30, "max_retry_delay": 30}
        if any(type(getattr(self,n)) is not int or not 1 <= getattr(self,n) <= maximum
               for n, maximum in bounds.items()):
            raise AcquisitionError("acquisition limits must be positive integers within hard safety bounds")


@dataclass(frozen=True)
class HTTPPage:
    status: int
    body: bytes = field(default=b"", repr=False)
    retry_after: str | None = field(default=None, repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward authentication to a redirect target.


def _http_get(url, headers, timeout, max_bytes):
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            chunks, count = [], 0
            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise AcquisitionError("response reading exceeded the time bound")
                chunk = response.read1(min(65536, max_bytes + 1 - count))
                count += len(chunk)
                if count > max_bytes:
                    raise AcquisitionError("response exceeds page byte limit")
                if not chunk:
                    break
                chunks.append(chunk)
            return HTTPPage(response.status, b"".join(chunks))
    except HTTPError as exc:
        status, retry = exc.code, (exc.headers or {}).get("Retry-After")
        exc.close()  # Error bodies are neither logged nor archived.
        return HTTPPage(status, retry_after=retry)
    except (URLError, OSError, TimeoutError):
        raise TransportError("market-data transport failed") from None


def acquisition_plan(start: str, end: str, *, feed="iex", limits=None, now=None):
    limits = limits or FetchLimits()
    start, end = _stamp(start), _stamp(end)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise AcquisitionError("acquisition clock must be timezone-aware")
    if feed not in ("iex", "sip") or start > end or end - start > timedelta(days=7):
        raise AcquisitionError("request requires iex/sip and an ordered interval of at most seven days")
    # A conservative historical-only margin avoids accidental recent-feed use.
    if end > now - timedelta(minutes=16):
        raise AcquisitionError("request end must be at least 16 minutes old")
    request = {"symbols": ["SPY", "QQQ"], "timeframe": "1Min", "start": start.isoformat(),
               "end": end.isoformat(), "feed": feed, "adjustment": "raw", "asof": "-",
               "currency": "USD", "sort": "asc", "limit": 10000}
    return {"endpoint": ENDPOINT, "request": request, "limits": asdict(limits),
            "maximum_http_attempts": limits.max_pages * limits.max_attempts,
            "method": "GET", "redirects": "rejected", "orders": False}


def _retry_delay(header, attempt, maximum, now):
    if header is None:
        delay = 2 ** attempt
    elif header.isdigit():
        delay = int(header)
    else:
        try:
            retry = parsedate_to_datetime(header)
            if retry.tzinfo is None:
                raise ValueError()
            delay = max(0, math.ceil((retry-now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            raise AcquisitionError("invalid Retry-After header") from None
    if delay > maximum:
        raise AcquisitionError("server retry delay exceeds configured wait bound")
    return delay


def acquire_bundle(start: str, end: str, destination: Path, *, feed="iex", limits=None,
                   credentials=None, transport=None, clock=None, sleeper=None):
    """Acquire all pages before publishing; injected transports are test-only.

    No entitlement fallback, subscription, order endpoint, or unbounded retry.
    Failure never publishes a truncated capture as a completed bundle.
    """
    limits = limits or FetchLimits()
    clock = clock or (lambda: datetime.now(timezone.utc))
    plan = acquisition_plan(start, end, feed=feed, limits=limits, now=clock())
    if destination.exists() or destination.is_symlink():
        raise AcquisitionError("destination already exists; choose a new directory")
    credentials = credentials or Credentials.from_environment()
    transport, sleeper = transport or _http_get, sleeper or time.sleep
    headers = {"APCA-API-KEY-ID": credentials.key, "APCA-API-SECRET-KEY": credentials.secret,
               "Accept": "application/json", "User-Agent": "orb-historical-acquisition/1"}
    pages, token, seen, total_bytes = [], None, set(), 0
    for _ in range(limits.max_pages):
        params = {**plan["request"], "symbols": "SPY,QQQ"}
        if token is not None:
            params["page_token"] = token
        url = ENDPOINT + "?" + urlencode(params)
        for attempt in range(limits.max_attempts):
            try:
                response = transport(url, headers, limits.timeout_seconds, limits.max_page_bytes)
            except (TransportError, URLError, OSError, TimeoutError):
                if attempt + 1 == limits.max_attempts:
                    raise AcquisitionError("market-data transport attempts exhausted") from None
                sleeper(_retry_delay(None, attempt, limits.max_retry_delay, clock()))
                continue
            if response.status == 200:
                break
            if response.status in (401, 403):
                raise AcquisitionError("market-data authentication or entitlement denied; no fallback attempted")
            if response.status not in (408, 429, 500, 502, 503, 504):
                raise AcquisitionError(f"market-data HTTP status {response.status}; request stopped")
            if attempt + 1 == limits.max_attempts:
                raise AcquisitionError("market-data HTTP retry attempts exhausted")
            sleeper(_retry_delay(response.retry_after, attempt, limits.max_retry_delay, clock()))
        body = response.body
        if len(body) > limits.max_page_bytes:
            raise AcquisitionError("response exceeds page byte limit")
        total_bytes += len(body)
        if total_bytes > limits.max_total_bytes:
            raise AcquisitionError("responses exceed total byte limit")
        if any(value.encode() in body for value in (credentials.key, credentials.secret)):
            raise AcquisitionError("response contains credential material; refusing archive")
        try:
            decoded = _decode(body)
            _fields(decoded, ("bars", "next_page_token"), ("currency",))
        except (ValueError, TypeError):
            raise AcquisitionError("invalid market-data response") from None
        pages.append(b'{"page_token":' + json.dumps(token).encode() + b',"response":' + body + b'}')
        token = decoded["next_page_token"]
        if token is None:
            break
        if not isinstance(token, str) or not token or len(token) > 4096 or token in seen:
            raise AcquisitionError("invalid or repeated continuation token")
        seen.add(token)
    else:
        raise AcquisitionError("page budget exhausted before token termination")
    metadata = {"schema_version": CAPTURE_SCHEMA, "provider": "alpaca", "data_kind": "historical",
                "request": plan["request"], "retrieved_at": clock().astimezone(timezone.utc).isoformat()}
    raw = json.dumps(metadata).encode()[:-1] + b',"pages":[' + b','.join(pages) + b']}'
    if len(raw) > limits.max_total_bytes:
        raise AcquisitionError("capture envelope exceeds total byte limit")
    try:
        return write_bundle(raw, destination)
    except ValueError:
        raise AcquisitionError("acquired capture failed strict validation") from None
