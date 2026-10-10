# ORB Trading System

A Python research and paper-trading project implementing Opening Range Breakout.

## Status

The first implementation validates completed one-minute bars and calculates a
15-minute opening range. Broker integration, breakout detection, risk sizing,
calendar validation, backtesting and paper trading are still planned.
This application currently submits no orders.

## Run locally

Python 3.11 or later is required. On macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

## Baseline

SPY/QQQ, long only, completed one-minute bars, range [09:30, 09:45)
America/New_York. Future signals use a close above the frozen range high before
11:00, midpoint stop and planned 2R target. See docs/specification.md.

## Documentation

- docs/project-report.md: motivation, design journey, evidence and limitations.
- docs/specification.md: intended behaviour and acceptance criteria.
- docs/0001-language-choice.md: Python choice and future C++ criteria.

## Limits

The range function expects an exchange session already validated by its caller.
It does not yet exclude holidays. Data supplied to it must represent completed
bars; the available_at argument prevents calculating the range before 09:45.
Tests are software evidence, not trading-performance evidence.
