from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from orb.acquisition import Credentials
from orb.paper_snapshot import (BASE_URL, HTTPResponse, PaperSnapshotError,
                                SnapshotLimits, capture_paper_snapshot, snapshot_plan)
from orb.reconciliation import ReconciliationLedger


NOW = datetime(2026, 10, 23, 18, 50, tzinfo=timezone.utc)
ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
CREDS = Credentials("paper-test-key", "paper-test-secret")


def account(**changes):
    value = {"id": ACCOUNT_ID, "cash": "9799.58", "status": "ACTIVE",
             "account_blocked": False, "trading_blocked": False,
             "trade_suspended_by_user": False, "extra_provider_field": "ignored"}
    value.update(changes)
    return value


def position(symbol="SPY", qty="2", side="long", asset_class="us_equity"):
    return {"symbol": symbol, "qty": qty, "side": side, "asset_class": asset_class,
            "current_price": "999.99"}


def order(identity="orb-entry"):
    return {"client_order_id": identity, "status": "new", "symbol": "SPY"}


def sequence(account_value=None, positions=None, orders=None):
    values = [account_value or account(), positions if positions is not None else [position()],
              orders if orders is not None else [order()]]
    return [HTTPResponse(200, json.dumps(value).encode()) for value in values + values]


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout, maximum):
        self.calls.append((url, dict(headers), timeout, maximum))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def capture(responses=None, **kwargs):
    transport = Transport(responses or sequence())
    result = capture_paper_snapshot(credentials=CREDS, transport=transport,
                                    clock=lambda: NOW, **kwargs)
    return result, transport


def test_two_identical_passes_produce_complete_redacted_broker_snapshot():
    result, transport = capture()
    payload = result.payload()
    assert result.complete
    assert payload["complete"] is True and payload["reasons"] == []
    assert payload["broker_snapshot"]["cash"] == "9799.58"
    assert payload["broker_snapshot"]["positions"] == {"SPY": 2}
    assert payload["broker_snapshot"]["open_client_order_ids"] == ["orb-entry"]
    assert payload["broker_snapshot"]["account_scope"].startswith("paper:alpaca:")
    rendered = json.dumps(payload)
    assert ACCOUNT_ID not in rendered and CREDS.key not in rendered and CREDS.secret not in rendered
    assert payload["orders_submitted"] is payload["paper_execution_enabled"] is False
    assert len(payload["response_sha256"]) == len(transport.calls) == 6


def test_requests_are_fixed_get_resources_with_unfiltered_open_orders():
    _, transport = capture()
    for index, (url, headers, timeout, maximum) in enumerate(transport.calls):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "paper-api.alpaca.markets"
        assert parsed.path == ("/v2/account", "/v2/positions", "/v2/orders")[index % 3]
        if parsed.path == "/v2/orders":
            assert parse_qs(parsed.query) == {
                "status": ["open"], "limit": ["500"], "direction": ["asc"],
                "nested": ["false"]}
            assert "symbols" not in parse_qs(parsed.query)
        else:
            assert not parsed.query
        assert headers["APCA-API-KEY-ID"] == CREDS.key
        assert headers["APCA-API-SECRET-KEY"] == CREDS.secret
        assert timeout == 10 and maximum == 1_000_000


def test_plan_needs_no_credentials_and_declares_exact_budget():
    plan = snapshot_plan()
    assert plan["base_url"] == BASE_URL and plan["method"] == "GET"
    assert plan["passes"] == 2 and plan["maximum_http_requests"] == 6
    assert plan["orders_submitted"] is plan["paper_execution_enabled"] is False
    assert plan["resources"][-1].endswith("status=open&limit=500&direction=asc&nested=false")


@pytest.mark.parametrize(("first_index", "replacement", "reason"), [
    (3, account(cash="9799.57"), "account_changed_between_passes"),
    (4, [position(qty="1")], "positions_changed_between_passes"),
    (5, [order("orb-other")], "orders_changed_between_passes"),
])
def test_changed_second_pass_is_explicitly_incomplete(first_index, replacement, reason):
    responses = sequence()
    responses[first_index] = HTTPResponse(200, json.dumps(replacement).encode())
    result, _ = capture(responses)
    assert not result.complete and reason in result.reasons
    assert result.snapshot is not None and result.snapshot.complete is False


@pytest.mark.parametrize(("changes", "reason"), [
    ({"status": "ACCOUNT_UPDATED"}, "account_not_active"),
    ({"account_blocked": True}, "account_blocked"),
    ({"trading_blocked": True}, "account_blocked"),
    ({"trade_suspended_by_user": True}, "account_blocked"),
])
def test_account_state_can_be_normalised_but_never_declared_complete(changes, reason):
    result, _ = capture(sequence(account(**changes)))
    assert result.snapshot is not None and not result.complete and reason in result.reasons


@pytest.mark.parametrize("item", [
    position("AAPL"), position(side="short"), position(asset_class="crypto"),
])
def test_unsupported_position_prevents_a_lossy_broker_snapshot(item):
    result, _ = capture(sequence(positions=[item]))
    assert result.snapshot is None and result.reasons == ("unsupported_position",)
    assert result.payload()["broker_snapshot"] is None


@pytest.mark.parametrize("quantity", ["0", "-1", "0.5", "2147483648"])
def test_unsupported_position_quantity_prevents_normalisation(quantity):
    result, _ = capture(sequence(positions=[position(qty=quantity)]))
    assert result.snapshot is None
    assert result.reasons == ("unsupported_position_quantity",)


def test_exact_order_limit_is_incomplete_because_more_rows_cannot_be_excluded():
    orders = [order(f"open-{index}") for index in range(3)]
    limits = SnapshotLimits(order_limit=3)
    result, _ = capture(sequence(orders=orders), limits=limits)
    assert not result.complete and result.snapshot is not None
    assert result.reasons == ("open_order_limit_reached",)


@pytest.mark.parametrize(("index", "body", "message"), [
    (0, b"[]", "account response"),
    (1, b"{}", "positions response"),
    (2, b"{}", "orders response"),
    (0, b'{"id":1}', "account"),
    (0, b'{"id":"x","id":"y"}', "JSON"),
    (0, b'{"cash":NaN}', "JSON"),
])
def test_malformed_provider_evidence_fails_closed(index, body, message):
    responses = sequence()
    responses[index] = HTTPResponse(200, body)
    with pytest.raises(PaperSnapshotError, match=message):
        capture(responses)


@pytest.mark.parametrize("identity", ["", "has spaces", "line\nbreak", "x" * 129])
def test_invalid_client_order_identity_is_not_silently_omitted(identity):
    with pytest.raises(PaperSnapshotError, match="client order identity"):
        capture(sequence(orders=[order(identity)]))


def test_duplicate_position_or_order_identity_is_rejected():
    with pytest.raises(PaperSnapshotError, match="duplicate paper position"):
        capture(sequence(positions=[position(), position()]))
    with pytest.raises(PaperSnapshotError, match="duplicate paper client order"):
        capture(sequence(orders=[order(), order()]))


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 429, 500])
def test_non_success_status_stops_without_a_second_request(status):
    transport = Transport([HTTPResponse(status, CREDS.secret.encode())])
    with pytest.raises(PaperSnapshotError) as error:
        capture_paper_snapshot(credentials=CREDS, transport=transport, clock=lambda: NOW)
    assert CREDS.secret not in str(error.value) and len(transport.calls) == 1


def test_response_limits_and_credential_echo_fail_without_leakage():
    limits = SnapshotLimits(max_response_bytes=10, max_total_bytes=60)
    with pytest.raises(PaperSnapshotError, match="byte limit"):
        capture([HTTPResponse(200, b"x" * 11)], limits=limits)
    responses = sequence()
    responses[0] = HTTPResponse(200, CREDS.secret.encode())
    with pytest.raises(PaperSnapshotError) as error:
        capture(responses)
    assert CREDS.secret not in str(error.value)


def test_total_response_limit_applies_across_all_six_gets():
    padded = account(extra_padding="x" * 500)
    responses = sequence(account_value=padded)
    limits = SnapshotLimits(max_response_bytes=1000, max_total_bytes=1000)
    with pytest.raises(PaperSnapshotError, match="total byte limit"):
        capture(responses, limits=limits)


def test_naive_or_backwards_clock_is_rejected():
    with pytest.raises(PaperSnapshotError, match="timezone-aware"):
        capture_paper_snapshot(credentials=CREDS, transport=Transport(sequence()),
                               clock=lambda: NOW.replace(tzinfo=None))
    times = iter((NOW, NOW - timedelta(seconds=1)))
    with pytest.raises(PaperSnapshotError, match="moved backwards"):
        capture_paper_snapshot(credentials=CREDS, transport=Transport(sequence()),
                               clock=lambda: next(times))


def test_cli_plan_is_network_free_and_explicit():
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "src"}
    result = subprocess.run([sys.executable, "-m", "orb", "paper-snapshot", "--plan"],
                            cwd=Path.cwd(), env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["maximum_http_requests"] == 6 and payload["method"] == "GET"
    assert payload["orders_submitted"] is False


def test_scope_is_stable_for_same_account_and_changes_for_another_account():
    first, _ = capture()
    other, _ = capture(sequence(account_value=account(id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")))
    assert first.snapshot.account_scope == capture()[0].snapshot.account_scope
    assert first.snapshot.account_scope != other.snapshot.account_scope


def test_complete_capture_feeds_existing_ledger_without_enabling_execution(tmp_path):
    result, _ = capture(sequence(account_value=account(cash="10000"), positions=[], orders=[]))
    with ReconciliationLedger(tmp_path / "ledger.sqlite", result.snapshot.account_scope,
                              D("10000")) as ledger:
        comparison = ledger.reconcile("paper-snapshot-1", result.snapshot)
        saved = ledger.snapshot()
    assert comparison["broker_reconciled"] is True
    assert comparison["paper_execution_enabled"] is False
    assert saved["broker_reconciled"] is True and saved["orders_submitted"] is False


def test_unstable_capture_is_durably_recorded_as_incomplete(tmp_path):
    responses = sequence(account_value=account(cash="10000"), positions=[], orders=[])
    responses[3] = HTTPResponse(200, json.dumps(account(cash="9999")).encode())
    result, _ = capture(responses)
    with ReconciliationLedger(tmp_path / "ledger.sqlite", result.snapshot.account_scope,
                              D("10000")) as ledger:
        comparison = ledger.reconcile("paper-snapshot-unstable", result.snapshot)
    assert comparison["broker_reconciled"] is False
    assert comparison["reasons"] == ["snapshot_incomplete", "cash_mismatch"]
    assert comparison["paper_execution_enabled"] is False


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": 31}, {"max_response_bytes": 2_000_001},
    {"max_total_bytes": 0}, {"order_limit": 501},
])
def test_safety_limits_have_hard_bounds(kwargs):
    with pytest.raises(PaperSnapshotError, match="hard safety bounds"):
        SnapshotLimits(**kwargs)


def test_invalid_transport_result_is_rejected():
    with pytest.raises(PaperSnapshotError, match="transport response"):
        capture_paper_snapshot(credentials=CREDS, transport=Transport([object()]),
                               clock=lambda: NOW)
