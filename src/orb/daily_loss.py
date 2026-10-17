"""In-memory session loss latch; no orders, liquidation or durable recovery."""

from datetime import datetime
from decimal import Decimal

from orb.risk import _decimal
from orb.session import ExchangeSession


class DailyLossGuard:
    """Fixed session-opening reference, not a trailing high-water mark.

    Missing reference or subsequent held-equity observations block the rest of
    the session. A new instance is required for the next exchange session;
    recovery within a session cannot erase a loss or data-uncertainty latch.
    """

    def __init__(self, session: ExchangeSession, loss_fraction: Decimal,
                 reference_equity: Decimal | None):
        _decimal("daily loss fraction", loss_fraction, positive=True)
        if loss_fraction > 1:
            raise ValueError("daily loss fraction must be at most 1")
        if reference_equity is not None:
            _decimal("reference equity", reference_equity, positive=True)
        self.session = session
        self.loss_fraction = loss_fraction
        self.reference_equity = reference_equity
        self.block_reason = "daily_loss_baseline_unavailable" if reference_equity is None else None
        self.blocked_at = session.opens_at if self.block_reason else None
        self.last_at = None
        self.equity = reference_equity

    def observe(self, at: datetime, equity: Decimal | None) -> dict:
        if (not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None
                or not self.session.opens_at <= at <= self.session.closes_at):
            raise ValueError("loss observation must be within its aware exchange session")
        if self.last_at is not None and at < self.last_at:
            raise ValueError("loss observations must be chronological")
        if equity is not None and (not isinstance(equity, Decimal) or not equity.is_finite()):
            raise ValueError("observed equity must be finite Decimal or unknown")
        self.last_at, self.equity = at, equity
        if self.block_reason is None:
            if equity is None:
                self.block_reason = "daily_loss_equity_unavailable"
            elif self.reference_equity - equity >= self.reference_equity * self.loss_fraction:
                self.block_reason = "daily_loss_limit"
            if self.block_reason:
                self.blocked_at = at
        return self.to_dict()

    def to_dict(self) -> dict:
        return {"session": str(self.session.day), "reference_equity": self.reference_equity,
                "loss_fraction": self.loss_fraction,
                "loss_limit": (self.reference_equity * self.loss_fraction
                               if self.reference_equity is not None else None),
                "equity": self.equity,
                "loss_from_open": (max(Decimal("0"), self.reference_equity - self.equity)
                                   if self.reference_equity is not None and self.equity is not None else None),
                "blocked": self.block_reason is not None, "reason": self.block_reason,
                "blocked_at": self.blocked_at, "last_observed_at": self.last_at}
