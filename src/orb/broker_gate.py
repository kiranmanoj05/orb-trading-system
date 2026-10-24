"""Conservative snapshot comparison for the offline full-fill coordinator.

No partial-fill import, repair, migration or dispatch is performed here. The
separate partial ledger cannot authorise this simpler account model.
"""

from datetime import datetime, timedelta
from decimal import Decimal


RECOVERY_HOLDS = frozenset(("order_status_unknown", "terminal_correction", "fill_bust", "external_activity"))
CAPTURE_FAILURES = frozenset(("capture_unavailable", "unsupported_positions", "capture_in_progress"))
MAX_SNAPSHOT_AGE = timedelta(seconds=60)


def compare_snapshot(account, account_scope, snapshot, at, journal_blocked):
    """Compare the shared BrokerSnapshot payload without changing cash/risk."""
    reasons = []
    if snapshot["account_scope"] != account_scope:
        reasons.append("account_scope_mismatch")
    if not snapshot["complete"]:
        reasons.append("snapshot_incomplete")
    age = at - datetime.fromisoformat(snapshot["as_of"])
    if not timedelta(0) <= age <= MAX_SNAPSHOT_AGE:
        reasons.append("snapshot_time_mismatch")
    if Decimal(snapshot["cash"]) != account.cash:
        reasons.append("cash_mismatch")
    positions = {}
    for allocation in account.allocations:
        if allocation.state in ("open", "unresolved"):
            positions[allocation.symbol] = positions.get(allocation.symbol, 0) + allocation.quantity
    if snapshot["positions"] != positions:
        reasons.append("position_mismatch")
    # A prepared intent has never been sent. All actual external lifecycle
    # states are unsupported by this account model and remain gated below.
    if snapshot["open_client_order_ids"]:
        reasons.append("unexpected_open_orders")
    if journal_blocked:
        reasons.append("external_lifecycle_unsupported")
    if any(a.entry_price is not None for a in account.allocations):
        reasons.append("synthetic_lifecycle")
    return reasons
