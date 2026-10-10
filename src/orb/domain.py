"""Validated immutable market-data contracts, independent of any broker."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    """A completed one-minute bar; start is the interval's aware timestamp."""

    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be non-empty, uppercase and trimmed")
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if self.start.second or self.start.microsecond:
            raise ValueError("start must align to a minute boundary")
        prices = (self.open, self.high, self.low, self.close)
        if any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in prices):
            raise ValueError("prices must be finite positive Decimal values")
        if self.high < max(prices) or self.low > min(prices):
            raise ValueError("high and low must bound all prices")
        if type(self.volume) is not int or self.volume < 0:
            raise ValueError("volume must be a non-negative integer")


@dataclass(frozen=True)
class OpeningRange:
    symbol: str
    start: datetime
    end: datetime
    high: Decimal
    low: Decimal

    @property
    def midpoint(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")
