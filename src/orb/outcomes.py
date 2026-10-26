"""Shared reporting taxonomy; uncertainty must never become a flat trade."""


KNOWN_NO_ENTRY_REASONS = frozenset({"entry_cutoff", "entry_at_or_below_stop"})


def outcome_class(trade: dict) -> str:
    """Classify saved portfolio records without changing execution decisions.

    Only explicitly understood no-entry reasons are eligible. New unfilled
    reasons and unknown statuses fail closed until their semantics are reviewed.
    A known skip must not carry a modeled fill or a nonzero share quantity.
    Rejections are known admission decisions, even if missing marks caused them;
    independent coverage and valuation gates still apply to account metrics.
    """
    status = trade.get("status")
    if status == "closed":
        return "closed"
    if status in ("rejected", "unfilled"):
        no_entry = trade.get("entry_at") is None and trade.get("quantity") == 0
        if no_entry and (status == "rejected" or trade.get("reason") in KNOWN_NO_ENTRY_REASONS):
            return "known_no_entry"
        return "uncertain_entry"
    if status == "unresolved":
        return "uncertain_position"
    return "unknown_outcome"


def outcomes_complete(trades) -> bool:
    return all(outcome_class(t) in ("closed", "known_no_entry") for t in trades)
