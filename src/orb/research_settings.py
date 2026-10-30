"""One explicit risk/cost contract for supported historical research controls."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from orb.entry_capacity import EntryCaps
from orb.risk import RiskLimits
from orb.simulation import ExecutionConfig
from orb.entry_policy import EntryPolicy

LEGACY_SCHEMA = 'orb-research-settings-v1'
SCHEMA = 'orb-research-settings-v2'
CAPS_SCHEMA = 'orb-research-settings-v3'
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
    max_session_entries: int = 2
    max_concurrent_positions: int = 2
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
        caps = EntryCaps(self.max_session_entries, self.max_concurrent_positions)
        if self.schema_version != CAPS_SCHEMA and caps != EntryCaps():
            raise ValueError("entry caps require explicit v3 research settings")
        policy = EntryPolicy(self.weekdays, self.target_r)
        if self.schema_version not in (LEGACY_SCHEMA, SCHEMA, CAPS_SCHEMA) or (self.schema_version == LEGACY_SCHEMA and not policy.is_reference):
            raise ValueError('non-reference entry settings require the explicit v2 schema')

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get('schema_version') not in (LEGACY_SCHEMA, SCHEMA, CAPS_SCHEMA):
            raise ValueError('unsupported research settings schema')
        schema = value['schema_version']
        required = {'schema_version', *(f[0] for f in FIELDS)}
        if schema in (SCHEMA, CAPS_SCHEMA):
            required |= {'weekdays', 'target_r'}
        if schema == CAPS_SCHEMA:
            required |= {'max_session_entries', 'max_concurrent_positions'}
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
        if schema in (SCHEMA, CAPS_SCHEMA):
            policy = EntryPolicy.from_dict({'schema_version': 'orb-entry-policy-v1',
                                             'weekdays': value['weekdays'], 'target_r': value['target_r']})
            numbers.update(weekdays=policy.weekdays, target_r=policy.target_r)
        if schema == CAPS_SCHEMA:
            for key in ("max_session_entries", "max_concurrent_positions"):
                if type(value[key]) is not str or value[key] not in ("1", "2"):
                    raise ValueError(f"{key} must be the integer string 1 or 2")
                numbers[key] = int(value[key])
        return cls(**numbers, schema_version=schema)

    @classmethod
    def from_config(cls, config):
        values = {f[0]: config[f[0]] for f in FIELDS}
        if 'entry_caps' in config:
            caps = EntryCaps.from_dict(config['entry_caps'])
            policy = EntryPolicy.from_dict(config['entry_policy'])
            values.update(target_r=canonical(policy.target_r), weekdays=list(policy.weekdays),
                          max_session_entries=str(caps.max_session_entries),
                          max_concurrent_positions=str(caps.max_concurrent_positions))
            return cls.from_dict({'schema_version': CAPS_SCHEMA, **values})
        if 'entry_policy' in config:
            policy = EntryPolicy.from_dict(config['entry_policy'])
            values.update(target_r=canonical(policy.target_r), weekdays=list(policy.weekdays))
            return cls.from_dict({'schema_version': SCHEMA, **values})
        return cls.from_dict({'schema_version': LEGACY_SCHEMA, **values})

    def to_dict(self):
        values = {'schema_version': self.schema_version, **{f[0]: canonical(getattr(self, f[0])) for f in FIELDS}}
        if self.schema_version in (SCHEMA, CAPS_SCHEMA):
            values.update(target_r=canonical(self.target_r), weekdays=list(self.weekdays))
        if self.schema_version == CAPS_SCHEMA:
            values.update(max_session_entries=str(self.max_session_entries),
                          max_concurrent_positions=str(self.max_concurrent_positions))
        return values

    def current_values(self):
        return {**self.to_dict(), 'schema_version': CAPS_SCHEMA,
                'max_session_entries': str(self.max_session_entries),
                'max_concurrent_positions': str(self.max_concurrent_positions),
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
                    **({'entry_policy': EntryPolicy(self.weekdays, self.target_r)} if self.schema_version in (SCHEMA, CAPS_SCHEMA) else {}),
                    **({"entry_caps": EntryCaps(self.max_session_entries, self.max_concurrent_positions)}
                       if self.schema_version == CAPS_SCHEMA else {}))


def settings_contract():
    return {'schema_version': CAPS_SCHEMA, 'defaults': ResearchSettings(schema_version=CAPS_SCHEMA).to_dict(),
            'fields': [dict(key=k, label=label, minimum=lo, maximum=hi, zero_allowed=zero,
                            minimum_inclusive=zero)
                       for k, label, _, lo, hi, zero in FIELDS] +
                      [dict(key='target_r', label='Planned target reward / risk (R)', minimum='0.25',
                            maximum='10', zero_allowed=False, minimum_inclusive=True),
                       *[dict(key=k, label=label, minimum='1', maximum='2', zero_allowed=False,
                              minimum_inclusive=True, integer=True) for k, label in
                         [('max_session_entries', 'Maximum filled entries per session (cap)'),
                          ('max_concurrent_positions', 'Maximum concurrent positions (pending included)')]]],
            'weekdays': [dict(value=i, label=d) for i, d in enumerate(('Mon', 'Tue', 'Wed', 'Thu', 'Fri'))],
            'unsupported': ['opening-range/entry timing', 'alternative stop policies',
                            'paper execution / external partial-fill accounting'],
            'notice': 'Research variants: weekdays gate entries only; target R is a price objective, '
                      'not a guaranteed net payoff. Entry limits are caps, never quotas; pending orders hold slots. '
                      'Reference settings remain fixed.'}
