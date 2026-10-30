"""Account-wide entry capacity; consume observations, never predict fills."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EntryCaps:
    # The implemented universe has at most one candidate per SPY/QQQ session.
    max_session_entries: int = 2
    max_concurrent_positions: int = 2

    def __post_init__(self):
        for value in (self.max_session_entries, self.max_concurrent_positions):
            if type(value) is not int or not 1 <= value <= 2:
                raise ValueError('entry/concurrency caps must be integers from 1 through 2')

    def to_dict(self):
        return {'schema_version': 'orb-entry-caps-v1',
                'max_session_entries': self.max_session_entries,
                'max_concurrent_positions': self.max_concurrent_positions}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != set(cls().to_dict())
                or value.get('schema_version') != 'orb-entry-caps-v1'):
            raise ValueError('unsupported entry caps contract')
        return cls(value['max_session_entries'], value['max_concurrent_positions'])


def validate_caps(caps):
    if caps is not None and not isinstance(caps, EntryCaps):
        raise ValueError('entry_caps must be validated EntryCaps or None')
    return caps


def capacity_usage(allocations, filled_sessions, day: date, caps: EntryCaps):
    """Caller owns atomicity and records a session on the first modeled fill.

    All pending reservations, including older sessions, reserve a possible entry
    today. A filled order counts once on its fill session even after closure.
    Closed positions release concurrency, never the filled-entry count. An
    unresolved holding remains concurrent. No-fill cancellation releases both.
    External partial evidence must retain its reservation AND reconciliation gate
    until the account model can represent and reconcile the actual remainder.
    """
    validate_caps(caps)
    if caps is None or type(day) is not date:
        raise ValueError('capacity usage requires explicit caps and a session date')
    active = [a for a in allocations if a.state in ('reserved', 'open', 'unresolved')]
    pending = sum(a.state == 'reserved' for a in active)
    filled = sum(d == day for d in filled_sessions.values())
    used = pending + filled
    reason = ('session_entry_limit' if used >= caps.max_session_entries else
              'concurrent_position_limit' if len(active) >= caps.max_concurrent_positions else None)
    return {'session': str(day), 'filled_entries': filled, 'pending_entry_slots': pending,
            'session_slots_used': used, 'concurrent_slots_used': len(active),
            'session_slots_remaining': max(0, caps.max_session_entries - used),
            'concurrent_slots_remaining': max(0, caps.max_concurrent_positions - len(active)),
            'reason': reason, 'limits': caps.to_dict()}
