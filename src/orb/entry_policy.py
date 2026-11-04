"""Explicit research variants; the fixed baseline remains the default."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

POLICY_V1 = 'orb-entry-policy-v1'
POLICY_V2 = 'orb-entry-policy-v2'
RANGE_MINUTES = (5, 15, 30)
ENTRY_DELAYS = (0, 5, 15, 30)
ENTRY_CUTOFFS = ('10:00', '10:30', '11:00', '11:30', '12:00')
STOP_POLICIES = ('range_midpoint', 'range_low')


@dataclass(frozen=True)
class EntryPolicy:
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    target_r: Decimal = Decimal('2')
    range_minutes: int = 15
    entry_delay_minutes: int = 0
    entry_cutoff_local: str = '11:00'
    stop_policy: str = 'range_midpoint'
    schema_version: str = POLICY_V1

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
        if (type(self.range_minutes) is not int or self.range_minutes not in RANGE_MINUTES
                or type(self.entry_delay_minutes) is not int or self.entry_delay_minutes not in ENTRY_DELAYS
                or self.entry_cutoff_local not in ENTRY_CUTOFFS or self.stop_policy not in STOP_POLICIES
                or self.schema_version not in (POLICY_V1, POLICY_V2)):
            raise ValueError('unsupported range, entry window or stop policy')
        cutoff = time.fromisoformat(self.entry_cutoff_local)
        available = 9 * 60 + 30 + self.range_minutes + self.entry_delay_minutes + 1
        if available >= cutoff.hour * 60 + cutoff.minute:
            raise ValueError('entry window must allow a completed bar strictly before cutoff')
        if self.schema_version == POLICY_V1 and not self.uses_reference_window:
            raise ValueError('timing and stop variants require the explicit v2 entry policy')

    @property
    def uses_reference_window(self):
        return (self.range_minutes == 15 and self.entry_delay_minutes == 0
                and self.entry_cutoff_local == '11:00' and self.stop_policy == 'range_midpoint')

    def range_end(self, session):
        return session.opens_at + timedelta(minutes=self.range_minutes)

    def entry_start(self, session):
        return self.range_end(session) + timedelta(minutes=self.entry_delay_minutes)

    def entry_cutoff(self, session):
        local = datetime.combine(session.day, time.fromisoformat(self.entry_cutoff_local),
                                 ZoneInfo('America/New_York'))
        return min(local, session.flatten_at).astimezone(timezone.utc)

    def stop(self, opening):
        return opening.midpoint if self.stop_policy == 'range_midpoint' else opening.low

    @property
    def is_reference(self):
        return self.weekdays == (0, 1, 2, 3, 4) and self.target_r == 2 and self.uses_reference_window

    def allows(self, session_date):
        if type(session_date) is not date:
            raise ValueError('entry eligibility requires a New York session date')
        return session_date.weekday() in self.weekdays

    def to_dict(self):
        r = format(self.target_r, 'f')
        return {'schema_version': self.schema_version, 'weekdays': list(self.weekdays),
                'target_r': r.rstrip('0').rstrip('.') if '.' in r else r,
                **(dict(range_minutes=str(self.range_minutes), entry_delay_minutes=str(self.entry_delay_minutes),
                        entry_cutoff_local=self.entry_cutoff_local, stop_policy=self.stop_policy)
                   if self.schema_version == POLICY_V2 else {})}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('unsupported entry policy contract')
        schema = value.get('schema_version')
        required = {'schema_version', 'weekdays', 'target_r'}
        if schema == POLICY_V2:
            required |= {'range_minutes', 'entry_delay_minutes', 'entry_cutoff_local', 'stop_policy'}
        if (set(value) != required or schema not in (POLICY_V1, POLICY_V2)
                or not isinstance(value['target_r'], str) or not 1 <= len(value['target_r']) <= 32
                or type(value['weekdays']) is not list):
            raise ValueError('unsupported entry policy contract')
        try:
            target = Decimal(value['target_r'])
        except InvalidOperation:
            raise ValueError('target R must be a decimal string') from None
        extra = {}
        if schema == POLICY_V2:
            for key, allowed in (('range_minutes', RANGE_MINUTES), ('entry_delay_minutes', ENTRY_DELAYS)):
                if type(value[key]) is not str or value[key] not in tuple(map(str, allowed)):
                    raise ValueError('unsupported timing setting: ' + key)
                extra[key] = int(value[key])
            if type(value['entry_cutoff_local']) is not str or type(value['stop_policy']) is not str:
                raise ValueError('timing and stop choices must be strings')
            extra.update(entry_cutoff_local=value['entry_cutoff_local'], stop_policy=value['stop_policy'])
        return cls(tuple(value['weekdays']), target, **extra, schema_version=schema)

    @property
    def strategy_version(self):
        if self.is_reference:
            return 'orb-long-v1'
        digest = sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return ('orb-long-variant-v2:' if self.schema_version == POLICY_V2 else 'orb-long-variant-v1:') + digest[:16]


def resolve_policy(value):
    if value is None:
        return EntryPolicy()
    if not isinstance(value, EntryPolicy):
        raise ValueError('entry policy must be a validated EntryPolicy')
    return value
