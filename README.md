# ORB Trading System

A Python research and paper-trading project implementing Opening Range Breakout.

## Status

The implementation validates completed one-minute bars, calculates a 15-minute
opening range and emits at most one long breakout candidate per symbol/session.
Broker integration, risk sizing, calendar validation, backtesting and paper
trading are still planned.
This application currently submits no orders.

## Run locally

Python 3.11 or later is required. On macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python -m orb
```

## Baseline

SPY/QQQ, long only, completed one-minute bars, range [09:30, 09:45)
America/New_York. Signals use a close strictly above the frozen range high,
observed before 11:00, midpoint stop and planned 2R target. The earliest eligible
bar starts at 09:45 and completes at 09:46. The signal uses the closing price as
an entry reference; no actual fill is implied.

## Candidate processing

`BreakoutDetector` consumes consecutive eligible bars and transitions from READY
to SIGNALLED, EXPIRED or PAUSED. Identical repeats of the latest consumed bar are
ignored. Conflicting corrections, older deliveries, missing eligible minutes
and stale bars pause candidate generation. The provisional freshness tolerance
is 90 seconds after bar completion. A candidate consumes the session attempt.

The synthetic demonstration uses fixed prices to produce range 98–100, entry
reference 100.20, stop 99 and planned target 102.60. It requires no credentials.

## Limits

The range function expects an exchange session already validated by its caller.
It does not yet exclude holidays. Data supplied to it must represent completed
bars; the available_at argument prevents calculating the range before 09:45.
Tests are software evidence, not trading-performance evidence.
Detector state is in memory and is not safe across restarts or concurrent event
workers. A stable signal ID identifies a candidate; persistent reservations and
broker reconciliation are required before claiming duplicate-order protection.
