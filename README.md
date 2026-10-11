# ORB Trading System

A Python research and paper-trading project implementing Opening Range Breakout.

## Status

The implementation validates completed one-minute bars, calculates a 15-minute
opening range and emits at most one long breakout candidate per symbol/session.
An offline CSV replay records candidates and per-session outcomes chronologically.
A fixed-share execution simulator records next-minute fills, exits and net P&L.
Broker integration, account risk sizing and paper trading are still planned.
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
python -m orb session 2026-11-27
python -m orb simulate fixtures/orb_execution_sample.csv --output results/simulation.json
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
fails explicitly. Premarket and bars starting at or after the actual session
close are ignored. Closed-date input is recorded as `closed` with no candidate.

Replay uses `exchange-calendars==4.13.2` and the XNYS regular-session schedule,
including holidays and early closes. It makes no fill/P&L claims.
Final corrected historical bars may differ from the originally streamed bars.

## Session calendar

The calendar covers 2006-01-01 through 2030-12-31. Dates outside this explicit
horizon fail rather than falling back to weekday hours. XNYS is the baseline US
equity session calendar for this SPY/QQQ research model, not per-venue routing.
The schedule is local, cached and versioned; it is not a live halt feed.

Session output includes the open, range end, entry cutoff, planned flatten time
and close. On 2026-11-27 the close is 13:00 New York time and the flatten plan is
12:55. The offline simulator uses this deadline for a market exit at the minute open;
no broker closing orders exist.
Candidate cutoff remains 11:00, or the flatten deadline if earlier.

## Limits

The standalone range function expects a session validated by its caller; the
replay now performs that check. Data supplied to it must represent completed
bars; the available_at argument prevents calculating the range before 09:45.
Tests are software evidence, not trading-performance evidence.
Detector state is in memory and is not safe across restarts or concurrent event
workers. A stable signal ID identifies a candidate; persistent reservations and
broker reconciliation are required before claiming duplicate-order protection.

## Simulated execution

`simulate` reuses replay signals and emits a trade ledger under model
`minute-long-v1`. The sample is synthetic: entry 100.30, stop 99, actual-fill
2R target 102.90 and gross/net gain 2.60 for one share with zero costs. This
checks arithmetic, not market performance. The original 16-bar replay fixture
has no entry bar and therefore produces an unfilled simulation attempt.

Entry occurs at the exact next minute open, strictly before the entry cutoff.
A missing entry bar or a fill at/below the stop cancels the attempt. The stop
stays at the range midpoint; the target is recalculated from the slipped entry.
Market buys add slippage; stop and flatten market sells subtract it. Targets
are simulated limit exits at the target or a better opening price, with no
adverse slippage. Stop gaps fill at the opening price less slippage. If both
stop and target are touched intrabar, stop wins and `ambiguous_exit` is true.
Open events precede intrabar extremes; scheduled flatten takes priority at
its deadline. Open fills carry the bar-start timestamp. Intrabar exit timestamps
are bar-completion observation times, not claimed exact execution seconds.

Use `--quantity 10 --slippage-bps 10 --fee-per-share 0.01` to declare costs.
Fees apply per share per side; slippage is a proportion of market-fill price.
JSON records configuration, model version, replay outcomes, fills, fees and
closed-trade gross/net P&L. Missing holding minutes and truncated positions are
`unresolved`, with no invented exit or P&L. Account equity, risk sizing, margin,
portfolio exposure and performance summaries are not implemented.

This model assumes instantaneous next-open execution and immediate bracket
activation after entry. It has no spread, queue, latency, volume capacity,
partial fills or broker fee schedule. Prices are unrounded Decimal values;
provider adjustments and tradable tick rules require later adapter work.
Multi-symbol positions are independent fixed-share scenarios, not a portfolio.
