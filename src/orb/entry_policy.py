"""Explicit research variants; the fixed baseline remains the default."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


@dataclass(frozen=True)
class EntryPolicy:
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    target_r: Decimal = Decimal('2')

    def __post_init__(self):
        if (type(self.weekdays) is not tuple or not self.weekdays
                or any(type(d) is not int or d not in range(5) for d in self.weekdays)
                or tuple(sorted(set(self.weekdays))) != self.weekdays):
            raise ValueError('weekdays must be nonempty, unique ordered NY weekdays 0–4')
        r = self.target_r
        if (not isinstance(r, Decimal) or not r.is_finite() or not Decimal('.25') <= r <= 10
                or abs(r.as_tuple().exponent) > 12 or len(r.as_tuple().digits) > 28):
            raise ValueError('target R must be a finite Decimal from 0.25 to 10')
        text = format(r, 'f')
        canonical = text.rstrip('0').rstrip('.') if '.' in text else text
        object.__setattr__(self, 'target_r', Decimal(canonical))

    @property
    def is_reference(self):
        return self.weekdays == (0, 1, 2, 3, 4) and self.target_r == 2

    def allows(self, session_date):
        if type(session_date) is not date:
            raise ValueError('entry eligibility requires a New York session date')
        return session_date.weekday() in self.weekdays

    def to_dict(self):
        r = format(self.target_r, 'f')
        return {'schema_version': 'orb-entry-policy-v1', 'weekdays': list(self.weekdays),
                'target_r': r.rstrip('0').rstrip('.') if '.' in r else r}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'weekdays', 'target_r'}
                or value['schema_version'] != 'orb-entry-policy-v1'
                or not isinstance(value['target_r'], str) or not 1 <= len(value['target_r']) <= 32
                or type(value['weekdays']) is not list):
            raise ValueError('unsupported entry policy contract')
        try:
            target = Decimal(value['target_r'])
        except InvalidOperation:
            raise ValueError('target R must be a decimal string') from None
        return cls(tuple(value['weekdays']), target)

    @property
    def strategy_version(self):
        if self.is_reference:
            return 'orb-long-v1'
        digest = sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return 'orb-long-variant-v1:' + digest[:16]


def resolve_policy(value):
    if value is None:
        return EntryPolicy()
    if not isinstance(value, EntryPolicy):
        raise ValueError('entry policy must be a validated EntryPolicy')
    return value
