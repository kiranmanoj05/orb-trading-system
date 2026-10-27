"""One explicit risk/cost contract for supported historical research controls."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from orb.risk import RiskLimits
from orb.simulation import ExecutionConfig

SCHEMA = 'orb-research-settings-v1'
# Fraction fields are fractions in storage, never percentages.
FIELDS = (
    ('initial_cash', 'Starting capital (USD)', '10000', '0', '1000000000000', False),
    ('risk_fraction', 'Risk per trade (fraction)', '0.005', '0', '1', False),
    ('max_position_fraction', 'Position allocation cap (fraction)', '0.25', '0', '1', False),
    ('max_gross_fraction', 'Gross allocation cap (fraction)', '1', '0', '1', False),
    ('max_total_risk_fraction', 'Aggregate initial stop risk (fraction)', '0.01', '0', '1', False),
    ('daily_loss_fraction', 'Daily-loss entry-block threshold (fraction)', '0.02', '0', '1', False),
    ('slippage_bps', 'Market slippage per side (bps)', '0', '0', '1000', True),
    ('fee_per_share', 'Fee per share per side (USD)', '0', '0', '1000', True),
)


def canonical(number):
    if number == 0:
        return '0'
    value = format(number, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


@dataclass(frozen=True)
class ResearchSettings:
    initial_cash: Decimal = Decimal('10000')
    risk_fraction: Decimal = Decimal('0.005')
    max_position_fraction: Decimal = Decimal('0.25')
    max_gross_fraction: Decimal = Decimal('1')
    max_total_risk_fraction: Decimal = Decimal('0.01')
    daily_loss_fraction: Decimal = Decimal('0.02')
    slippage_bps: Decimal = Decimal('0')
    fee_per_share: Decimal = Decimal('0')

    def __post_init__(self):
        for key, _, _, lower, upper, inclusive in FIELDS:
            n = getattr(self, key)
            if (not isinstance(n, Decimal) or not n.is_finite() or n > Decimal(upper)
                    or n < Decimal(lower) or (not inclusive and n == Decimal(lower))
                    or abs(n.as_tuple().exponent) > 12 or len(n.as_tuple().digits) > 28):
                raise ValueError(f'invalid supported setting: {key}')
        RiskLimits(self.risk_fraction, self.max_position_fraction,
                   self.max_gross_fraction, self.max_total_risk_fraction)
        ExecutionConfig(slippage_bps=self.slippage_bps, fee_per_share=self.fee_per_share)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'schema_version', *(f[0] for f in FIELDS)}:
            raise ValueError('settings must contain exactly the supported schema fields')
        if value['schema_version'] != SCHEMA:
            raise ValueError('unsupported research settings schema')
        numbers = {}
        for key, *_ in FIELDS:
            if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 32:
                raise ValueError(f'{key} must be a bounded decimal string')
            try:
                numbers[key] = Decimal(value[key])
            except InvalidOperation:
                raise ValueError(f'invalid supported setting: {key}') from None
        return cls(**numbers)

    @classmethod
    def from_config(cls, config):
        return cls.from_dict({'schema_version': SCHEMA, **{f[0]: config[f[0]] for f in FIELDS}})

    def to_dict(self):
        return {'schema_version': SCHEMA, **{k: canonical(v) for k, v in asdict(self).items()}}

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def engine_kwargs(self):
        return dict(initial_cash=self.initial_cash,
                    limits=RiskLimits(self.risk_fraction, self.max_position_fraction,
                                      self.max_gross_fraction, self.max_total_risk_fraction),
                    daily_loss_fraction=self.daily_loss_fraction,
                    slippage_bps=self.slippage_bps, fee_per_share=self.fee_per_share)


def settings_contract():
    return {'schema_version': SCHEMA, 'defaults': ResearchSettings().to_dict(),
            'fields': [dict(key=k, label=label, minimum=lo, maximum=hi, zero_allowed=zero)
                       for k, label, _, lo, hi, zero in FIELDS],
            'unsupported': ['weekday selection', 'target R changes', 'opening-range/entry timing',
                            'alternative stop policies', 'session-entry/concurrency caps'],
            'notice': 'Research risk/cost controls only. Reference signal rules remain fixed. '
                      'Caps block admissions; they do not guarantee losses or force trades.'}
