"""Offline Alpaca saved-page ingestion. No credentials, HTTP or broker calls."""

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re

from orb.domain import Bar
from orb.replay import CSV_FIELDS


CAPTURE_SCHEMA = "orb-alpaca-capture-v1"
BUNDLE_SCHEMA = "orb-historical-bundle-v1"
ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _decode(raw: bytes):
    return json.loads(raw.decode("utf-8"), parse_float=Decimal,
                      parse_constant=_invalid_constant, object_pairs_hook=_object)


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("unexpected or missing capture fields")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an aware string")
    # Do not silently truncate nonzero nanoseconds unsupported by datetime.
    fraction = re.search(r"\.([0-9]+)", value)
    if fraction and any(c != "0" for c in fraction[1][6:]):
        raise ValueError("unsupported sub-microsecond timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def _price(value):
    if type(value) not in (int, Decimal):
        raise ValueError("prices must be JSON numbers, not strings or booleans")
    return Decimal(value)


def _volume(value):
    number = _price(value)
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("volume must be a non-negative integer")
    return int(number)


def _decimal(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _json(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _digest(value):
    return sha256(_json(value)).hexdigest()


def normalise_capture(raw: bytes):
    """Validate an explicit request and complete token chain before sorting.

    Provider symbol/time ordering is checked independently of the chronological
    replay order. Identical duplicates are errors, never silently deduplicated.
    Request metadata and acquisition time remain assertions, not authentication.
    """
    capture = _decode(raw)
    _fields(capture, ("schema_version", "provider", "data_kind", "request", "retrieved_at", "pages"))
    if capture["schema_version"] != CAPTURE_SCHEMA or capture["provider"] != "alpaca":
        raise ValueError("unsupported capture schema/provider")
    if capture["data_kind"] not in ("synthetic", "historical"):
        raise ValueError("declare synthetic or historical data kind")
    request = capture["request"]
    _fields(request, ("symbols", "timeframe", "start", "end", "feed", "adjustment", "asof", "currency", "sort", "limit"))
    if request["symbols"] != ["SPY", "QQQ"]:
        raise ValueError("request must explicitly select SPY and QQQ")
    if (request["timeframe"] != "1Min" or request["adjustment"] != "raw" or
            request["asof"] != "-" or request["currency"] != "USD" or request["sort"] != "asc" or
            request["feed"] not in ("iex", "sip")):
        raise ValueError("baseline requires explicit 1Min/raw/USD/asc/asof=- and iex or sip")
    if type(request["limit"]) is not int or not 1 <= request["limit"] <= 10000:
        raise ValueError("page limit must be 1..10000")
    start, end, retrieved = map(_stamp, (request["start"], request["end"], capture["retrieved_at"]))
    if start > end or end + timedelta(minutes=1) > retrieved:
        raise ValueError("request must end before capture with completed minutes")
    pages = capture["pages"]
    if not isinstance(pages, list) or not pages:
        raise ValueError("capture needs response pages")
    bars, expected_token, seen_tokens, previous = [], None, set(), None
    for index, page in enumerate(pages):
        _fields(page, ("page_token", "response"))
        if page["page_token"] != expected_token or (index and expected_token is None):
            raise ValueError("broken pagination chain")
        response = page["response"]
        _fields(response, ("bars", "next_page_token"), ("currency",))
        if response.get("currency", "USD") != "USD":
            raise ValueError("response currency disagrees with request")
        groups = response["bars"]
        if groups is None:
            groups = {}
        if not isinstance(groups, dict) or set(groups) - {"SPY", "QQQ"}:
            raise ValueError("unexpected response symbols")
        page_count = 0
        for symbol, rows in sorted(groups.items()):
            if not isinstance(rows, list):
                raise ValueError("symbol bars must be an array")
            for row in rows:
                _fields(row, ("t", "o", "h", "l", "c", "v"), ("n", "vw"))
                stamp = _stamp(row["t"])
                bar = Bar(symbol, stamp, *(_price(row[n]) for n in ("o", "h", "l", "c")), _volume(row["v"]))
                if not start <= stamp <= end or stamp + timedelta(minutes=1) > retrieved:
                    raise ValueError("bar outside request bounds or unfinished at capture")
                key = (symbol, stamp)
                if previous is not None and key <= previous:
                    raise ValueError("duplicate or out-of-order provider bars")
                previous = key
                bars.append(bar)
                page_count += 1
        if page_count > request["limit"]:
            raise ValueError("response exceeds requested page limit")
        expected_token = response["next_page_token"]
        if expected_token is not None:
            if not isinstance(expected_token, str) or not expected_token or expected_token in seen_tokens:
                raise ValueError("invalid or repeated pagination token")
            seen_tokens.add(expected_token)
    if expected_token is not None:
        raise ValueError("truncated pagination: another response page is required")
    if not bars:
        raise ValueError("capture contains no bars; do not infer no-signal sessions")
    bars.sort(key=lambda b: (b.start, b.symbol))
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for bar in bars:
        writer.writerow([bar.symbol, bar.start.isoformat(),
                         *(_decimal(getattr(bar, n)) for n in ("open", "high", "low", "close")), bar.volume])
    canonical = buffer.getvalue().encode()
    metadata = {"schema_version": BUNDLE_SCHEMA, "adapter_version": "alpaca-saved-pages-v1",
                "endpoint": ENDPOINT, "provider": "alpaca", "data_kind": capture["data_kind"],
                "request": {**request, "start": start.isoformat(), "end": end.isoformat()},
                "retrieved_at": retrieved.isoformat(), "page_count": len(pages), "bars": len(bars),
                "capture_sha256": sha256(raw).hexdigest(), "csv_sha256": sha256(canonical).hexdigest(),
                "provider_authenticity_verified": False, "point_in_time_verified": False,
                "policy": "inclusive bar-start bounds; exact decimals; chronological UTC; no gap filling"}
    return bars, canonical, {**metadata, "bundle_id": _digest(metadata)}


def import_capture(source: Path, destination: Path) -> dict:
    raw = source.read_bytes()
    _, canonical, manifest = normalise_capture(raw)  # Validate before creating output.
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # Exclusive; never replace even an empty or symlinked target.
    # Manifest is the last completion marker. A failed write leaves a partial
    # directory for inspection; readers require all bytes and rederive evidence.
    for name, content in (("capture.json", raw), ("bars.csv", canonical), ("manifest.json", _json(manifest))):
        with (destination / name).open("xb") as handle:
            handle.write(content)
    return manifest


def load_bundle(directory: Path):
    paths = [directory / n for n in ("capture.json", "bars.csv", "manifest.json")]
    if directory.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError("bundle symlinks are not supported")
    raw, canonical, manifest_bytes = (p.read_bytes() for p in paths)
    bars, expected_csv, expected_manifest = normalise_capture(raw)
    if canonical != expected_csv or _decode(manifest_bytes) != expected_manifest:
        raise ValueError("bundle integrity mismatch")
    return bars, expected_manifest


def evaluate_bundle(directory: Path, *args, **kwargs):
    from orb.evaluation import _digest as evaluation_digest, evaluate

    bars, manifest = load_bundle(directory)
    if kwargs.get("data_label", "unspecified") == "unspecified":
        kwargs["data_label"] = manifest["data_kind"] + "-alpaca-capture-unverified"
    result = evaluate(bars, *args, **kwargs)
    result["manifest"]["base_schema_version"] = result["manifest"]["schema_version"]
    result["manifest"]["schema_version"] = "orb-evaluation-bundle-v1"
    result["manifest"]["dataset"]["source_bundle"] = {**manifest, "integrity_verified": True}
    result["evaluation_id"] = evaluation_digest(result["manifest"])
    result["summary"]["limitations"].append(
        "Archive integrity is checked; request labels, provider authenticity and point-in-time availability are unverified.")
    return result
