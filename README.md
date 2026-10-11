# ORB Trading System

A Python research and paper-trading project implementing Opening Range Breakout.

## Status

The implementation validates completed one-minute bars, calculates a 15-minute
opening range and emits at most one long breakout candidate per symbol/session.
An offline CSV replay records candidates and per-session outcomes chronologically.
Broker integration, risk sizing, calendar validation, simulated fills and paper
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
python -m orb replay fixtures/orb_sample.csv --output results/replay.json
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

## Offline replay

The sample CSV contains synthetic prices, not historical market observations.
Canonical CSV columns are `symbol,start,open,high,low,close,volume`. Timestamps
must be timezone-aware and minute-aligned. Input must be globally chronological;
different symbols can share a timestamp, but duplicate symbol/time keys are
rejected. Provider responses sorted by symbol must be normalised before replay.

The replay clock observes each bar at its end. Only consumed range bars build
the opening range. A JSON report records candidates and session states; prices
remain decimal strings. `ready` at dataset end means the observation window is
unfinished, not a confirmed whole session without a signal. Empty/invalid data
fails explicitly. Premarket and bars at or after 16:00 New York time are ignored.

Replay currently assumes externally validated exchange days and normal session
hours. It has no holiday or early-close calendar and makes no fill/P&L claims.
Final corrected historical bars may differ from the originally streamed bars.

## Limits

The range function expects an exchange session already validated by its caller.
It does not yet exclude holidays. Data supplied to it must represent completed
bars; the available_at argument prevents calculating the range before 09:45.
Tests are software evidence, not trading-performance evidence.
Detector state is in memory and is not safe across restarts or concurrent event
workers. A stable signal ID identifies a candidate; persistent reservations and
broker reconciliation are required before claiming duplicate-order protection.
