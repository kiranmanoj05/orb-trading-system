"""One explicit risk/cost contract for supported historical research controls."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from orb.risk import RiskLimits
from orb.simulation import ExecutionConfig
from orb.entry_policy import EntryPolicy

LEGACY_SCHEMA = 'orb-research-settings-v1'
SCHEMA = 'orb-research-settings-v2'
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
    target_r: Decimal = Decimal('2')
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    schema_version: str = LEGACY_SCHEMA

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
        policy = EntryPolicy(self.weekdays, self.target_r)
        if self.schema_version not in (LEGACY_SCHEMA, SCHEMA) or (self.schema_version == LEGACY_SCHEMA and not policy.is_reference):
            raise ValueError('non-reference entry settings require the explicit v2 schema')

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get('schema_version') not in (LEGACY_SCHEMA, SCHEMA):
            raise ValueError('unsupported research settings schema')
        schema = value['schema_version']
        required = {'schema_version', *(f[0] for f in FIELDS)}
        if schema == SCHEMA:
            required |= {'weekdays', 'target_r'}
        if set(value) != required:
            raise ValueError('settings must contain exactly the supported schema fields')
        numbers = {}
        for key, *_ in FIELDS:
            if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 32:
                raise ValueError(f'{key} must be a bounded decimal string')
            try:
                numbers[key] = Decimal(value[key])
            except InvalidOperation:
                raise ValueError(f'invalid supported setting: {key}') from None
        if schema == SCHEMA:
            policy = EntryPolicy.from_dict({'schema_version': 'orb-entry-policy-v1',
                                             'weekdays': value['weekdays'], 'target_r': value['target_r']})
            numbers.update(weekdays=policy.weekdays, target_r=policy.target_r)
        return cls(**numbers, schema_version=schema)

    @classmethod
    def from_config(cls, config):
        values = {f[0]: config[f[0]] for f in FIELDS}
        if 'entry_policy' in config:
            policy = EntryPolicy.from_dict(config['entry_policy'])
            values.update(target_r=canonical(policy.target_r), weekdays=list(policy.weekdays))
            return cls.from_dict({'schema_version': SCHEMA, **values})
        return cls.from_dict({'schema_version': LEGACY_SCHEMA, **values})

    def to_dict(self):
        values = {'schema_version': self.schema_version, **{f[0]: canonical(getattr(self, f[0])) for f in FIELDS}}
        if self.schema_version == SCHEMA:
            values.update(target_r=canonical(self.target_r), weekdays=list(self.weekdays))
        return values

    def current_values(self):
        return {**self.to_dict(), 'schema_version': SCHEMA,
                'target_r': canonical(self.target_r), 'weekdays': list(self.weekdays)}

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def engine_kwargs(self):
        return dict(initial_cash=self.initial_cash,
                    limits=RiskLimits(self.risk_fraction, self.max_position_fraction,
                                      self.max_gross_fraction, self.max_total_risk_fraction),
                    daily_loss_fraction=self.daily_loss_fraction,
                    slippage_bps=self.slippage_bps, fee_per_share=self.fee_per_share,
                    **({'entry_policy': EntryPolicy(self.weekdays, self.target_r)} if self.schema_version == SCHEMA else {}))


def settings_contract():
    return {'schema_version': SCHEMA, 'defaults': ResearchSettings(schema_version=SCHEMA).to_dict(),
            'fields': [dict(key=k, label=label, minimum=lo, maximum=hi, zero_allowed=zero,
                            minimum_inclusive=zero)
                       for k, label, _, lo, hi, zero in FIELDS] +
                      [dict(key='target_r', label='Planned target reward / risk (R)', minimum='0.25',
                            maximum='10', zero_allowed=False, minimum_inclusive=True)],
            'weekdays': [dict(value=i, label=d) for i, d in enumerate(('Mon', 'Tue', 'Wed', 'Thu', 'Fri'))],
            'unsupported': ['opening-range/entry timing', 'alternative stop policies',
                            'session-entry/concurrency caps'],
            'notice': 'Research variants: weekdays gate entries only; target R is a price objective, '
                      'not a guaranteed net payoff. Reference settings remain fixed.'}
