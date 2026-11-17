"""Bounded historical execution scenarios, separate from saved strategy presets."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FillAssumptions:
    """Delay a hypothetical market entry and add half a quoted spread per side.

    OHLCV does not measure quotes. This is sensitivity, never a broker emulator.
    The signal stays fixed; no order/reservation exists during the delay. Entry
    sizing occurs at the delayed open using only then-available account state.
    """
    entry_delay_minutes: int = 0
    spread_bps: Decimal = Decimal('0')  # full spread, not the per-side charge

    def __post_init__(self):
        if type(self.entry_delay_minutes) is not int or not 0 <= self.entry_delay_minutes <= 5:
            raise ValueError('entry delay must be an integer from 0 to 5 minutes')
        if (not isinstance(self.spread_bps, Decimal) or not self.spread_bps.is_finite()
                or not 0 <= self.spread_bps <= 100):
            raise ValueError('full spread must be a finite Decimal from 0 to 100 bps')

    def market_side_bps(self, slippage_bps):
        result = slippage_bps + self.spread_bps / 2
        if result >= 10000:
            raise ValueError('combined market-side cost must be below 10000 bps')
        return result

    def to_dict(self):
        return dict(schema_version='orb-fill-assumptions-v1',
                    entry_delay_minutes=self.entry_delay_minutes,
                    spread_bps=str(self.spread_bps),
                    spread_convention='half full spread added to slippage per market side; limit targets unchanged',
                    pending_policy='no order or reservation before delayed-open admission',
                    missing_wait_bar='uncertain entry; never jump missing minutes')


def validate_fill_assumptions(value):
    if value is not None and type(value) is not FillAssumptions:
        raise ValueError('fill assumptions must be a FillAssumptions instance or None')
    return value
