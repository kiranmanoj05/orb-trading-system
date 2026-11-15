# ORB — Explainable Trading Research & Paper Execution

An end-to-end Python and React platform for researching an opening-range
breakout strategy and observing its execution in an Alpaca paper account.

## The problem

A backtest can look convincing while hiding missing candles, unrealistic fills,
transaction costs or unresolved trades. Connecting it to a broker introduces
another challenge: a timed-out request may already have created an order.

I built ORB to make those assumptions and state transitions inspectable, from
the source candles to strategy decisions, simulated outcomes and saved broker
evidence.

## What I built

- A Python research engine with exchange-session handling, configurable risk
  limits, chronological account simulation and reproducible source identities.
- A React/TypeScript dashboard with interactive candles, trade inspection,
  versioned presets and durable research jobs behind a local FastAPI service.
- A bounded SPY paper-execution worker with bracket exits, durable order intents,
  risk reservations, duplicate prevention and restart inventory checks.
- Performance reporting with explicit cost assumptions, benchmark comparisons
  and session-block uncertainty, plus separately labelled paper cost estimates.
- SQLite evidence journals that preserve corrections and uncertain outcomes
  rather than silently replacing them with successful results.

## Engineering decisions

Historical simulations and paper-account activity use separate views and
balances. Chart fill markers require broker evidence; pending orders are not
displayed as executed trades. Missing data withholds aggregate performance.

The worker saves its intent before sending an order. If a response is uncertain,
it looks for that identity instead of blindly submitting again. Restart checks
compare fresh broker observations with saved state, and unresolved corrections
block automatic actions.

Costs have explicit provenance. An estimate based on preset assumptions is
labelled separately from broker-reported net profit. Account fees are not
assigned to individual trades merely because their dates appear related.

## Stack

Python · FastAPI · SQLite · React · TypeScript · esbuild · pytest · Playwright ·
Alpaca paper API

## Evaluation status

Automated tests exercise injected market and broker responses, including
partial fills, cancellation races, unknown submissions and process restarts.
These establish software behaviour, not broker performance or profitability.

Actual account execution acceptance and a broad, cost-aware strategy evaluation
remain pending. Complete attributable broker fees/net profit and reviewed
correction clearance are not yet implemented. Historical research and paper
execution also have different instrument scopes and fill assumptions.

The project demonstrates research infrastructure, backend/frontend integration,
stateful execution engineering and honest reporting. It does **not** currently
claim a profitable strategy or readiness for live capital.

## Evidence to add after validation

- A short demo: real candles → decision → paper order → confirmed fill → exit.
- A downloadable report identifying dates, data feed, exact configuration and
  cost assumptions, including losing and missing-data outcomes.
- Net return, expectancy, drawdown, trade/session counts and uncertainty from
  that report, with retrospective and prospective results distinguished.

Do not replace these items with invented numbers or synthetic screenshots
presented as market results. Link the source repository and describe this as a
research and paper-execution project while validation is in progress.
