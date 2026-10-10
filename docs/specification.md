# ORB Automated Trading System Technical Specification

Technical specification • 10 October 2026 • Version 1.1

## Purpose and provenance

Build an event-driven Python system that evaluates an Opening Range Breakout strategy, backtests it, and executes it in an Alpaca paper account. PostgreSQL provides an audit trail. The objective is a working, explainable engineering project with reproducible evidence, rather than an assumed profitable strategy.

This specification defines the intended MVP behaviour. Additional operational thresholds are labelled proposed.

## 1 Baseline strategy

| Setting | Version 1 rule |
|---|---|
| Instruments | SPY and QQQ |
| Direction | Long only |
| Time zone | America/New_York, with UTC timestamps persisted |
| Input | Completed one-minute OHLCV bars |
| Opening range | Bars starting at 09:30 through 09:44; interval [09:30, 09:45) |
| Range high and low | Maximum high and minimum low of those 15 bars |
| Entry trigger | First eligible completed bar whose close is strictly above the frozen range high |
| Earliest eligible bar | Bar starting 09:45, observed after it completes at 09:46 |
| Entry cutoff | Signal must be available before 11:00 local exchange time |
| Stop | Midpoint of the opening range |
| Target | Planned entry plus twice the planned entry-to-stop distance |
| Risk budget | 0.5% of account equity per trade |
| Daily frequency | One entry attempt per symbol per session |
| End of day | Begin flattening at 15:55 on normal sessions; no overnight positions intended |

Use the exchange calendar, not weekday checks. On early-close days, flatten five minutes before the actual close. On missing or invalid opening-range bars, skip that symbol for the day. Do not silently compute a range from an incomplete window. Rejected entries consume the entry attempt in the conservative MVP; later retry policy requires its own specification.

The signal is generated after a bar closes. It cannot fill at that same historical close by assumption. Backtesting schedules entry for the following bar's open with configurable adverse slippage. Paper trading submits after the signal is available, so its actual fill will differ.

The 2R target is planned, not guaranteed. Market-entry slippage changes realised risk and reward. Log both planned and filled values. Do not claim that a stop guarantees the risk budget: gaps, liquidity and execution failures can produce larger losses.

### Worked example

Range high = $100, low = $98, midpoint stop = $99. A completed eligible bar closes at $100.20. With $10,000 equity, planned risk budget is $50. At a planned entry of $100.20, risk per share is $1.20, so raw size is floor(50 / 1.20) = 41 shares before buying-power and exposure constraints. Planned target is $102.60. An actual fill at $100.30 increases the stop distance; store that difference and do not represent the original sizing as an exact realised risk limit.

These numbers demonstrate software behaviour; they are not recommended investment allocations.

## 2 Scope and completion

The MVP includes deterministic strategy logic, validated historical ingestion, a replay engine, realistic-enough documented fill assumptions, a paper execution adapter, risk gates, persistent order intent, restart reconciliation and basic reporting.

ML filters, short selling, futures, multi-account trading, a large dashboard and real-money execution are later extensions. The initial project must run without broker credentials using local fixtures. Real-money order submission is outside this build stage.

Completion means a reviewer can install the project, run tests, replay a documented dataset, inspect signals and trades, and understand failure behaviour from the README. Paper-trading claims require actual paper-run evidence. Strategy profitability requires separate out-of-sample results and must not be inferred from software correctness.

## 3 Architecture and responsibilities

| Component | Responsibility | Reason for separation |
|---|---|---|
| Domain models | Bars, sessions, signals, order intents, fills | Stable contracts shared by all adapters |
| Market-data adapter | Fetch history and receive streamed bars | Provider details stay outside strategy |
| Session service | Calendar, session boundaries, entry cutoff | One source of truth for time handling |
| ORB strategy | Build range and emit candidate signals | Pure decision logic can be replayed |
| Risk engine | Validate candidate and calculate size | A strategy cannot bypass account controls |
| Execution adapter | Submit, query and cancel broker orders | Simulation and Alpaca expose the same interface |
| Order manager | Persist intent and manage lifecycle | Signals and executed trades are different events |
| Reconciler | Compare broker and local state | Recovery must handle uncertain outcomes |
| Repository | Store events and projections | Decisions remain inspectable after restart |
| Reporting | Equity curve, statistics, run metadata | Results can be reproduced and reviewed |

Use one Python process with explicit modules initially. Microservices add deployment and distributed-consistency problems before they solve a demonstrated need. A bounded asynchronous queue can decouple streaming from processing; queue overflow must pause new entries rather than silently drop bars.

FastAPI is added for status and control only after the core works. The REST API does not contain strategy rules. A React interface is optional later; command-line reports are sufficient for the MVP.

## 4 Data contracts and time

Bar fields: symbol, UTC-aware start timestamp, interval, open, high, low, close, volume, feed and ingestion time. Validate positive prices, non-negative volume, high at least every other price, low at most every other price, supported interval and timestamp alignment.

Canonical key: symbol, feed, interval, start timestamp. Duplicate deliveries must not produce duplicate decisions. A correction to an already-consumed bar is logged; it does not retroactively rewrite a paper trade. Research datasets may use final corrected bars, but that difference must be disclosed because it can change signals.

Use Decimal for domain prices and monetary arithmetic. Round order prices according to supported instrument increments at the adapter boundary. NumPy floating-point arrays are suitable for analytics, with explicit conversion. Time values must be timezone-aware; London time must never be used as a fixed offset from New York.

Record dataset provider, feed, adjustment policy, retrieval date, time span, missing-data rules and checksum. Historical and streaming feeds should match where possible. A limited exchange feed and consolidated market feed can create different ranges; do not present them as equivalent inputs.

## 5 Strategy state machine

Per symbol and session: COLLECTING_RANGE → READY → ENTRY_REQUESTED → IN_POSITION → DONE. Add SKIPPED and PAUSED outcomes with recorded reasons. Broker updates, rather than signal generation, move a position into IN_POSITION.

At 09:45, freeze the range after verifying all 15 expected bars. Ignore entry candidates during range collection. In READY, compare the completed bar close with the range high and apply the cutoff. Emit a signal containing a deterministic ID, symbol, session, signal timestamp, planned entry reference, stop and strategy version. Reserve the daily attempt before execution so two concurrent handlers cannot submit separate entries.

A breakout candidate is a decision proposal, not an order. This distinction allows the risk engine to reject a valid strategy signal when account or operational conditions are unsuitable.

## 6 Risk rules

Position size is the minimum of risk-based size, affordable size, remaining symbol exposure and remaining portfolio exposure. All calculations use a documented entry reference plus a proposed slippage allowance. If quantity is zero, reject. Reject a stop at or above a long entry reference.

Proposed initial engineering defaults, to be configurable and tested: no leverage in our sizing logic, maximum 25% of equity in one symbol, maximum 50% total gross exposure, maximum two open positions, and a 1% session loss gate. These are provisional engineering defaults pending validation.

Define the session loss gate against session-start equity using marked equity, including realised and unrealised changes. At the threshold, stop new entries and initiate the documented flatten procedure. Outstanding entry orders count toward reserved exposure. Limits apply atomically when reserving an order, not merely when generating a signal. Stop orders and closing orders remain permitted when new entries are paused.

Pause on stale data, missing bars, reconciliation mismatch, rejected protective orders, disconnected order updates or exhausted bounded retries. Proposed freshness tolerance: latest expected completed bar must arrive within 90 seconds of its end. Distinguish broker connectivity from market-data freshness.

The kill switch has two explicit controls: pause entries, and cancel entries plus flatten. Persist the latch so a restart cannot silently resume. Flattening means request cancellation, resolve outstanding fills, close the remaining position, and verify the final broker state. Do not mark completion simply because a close request was accepted.

## 7 Order execution and recovery

Use Alpaca paper endpoints and a dedicated adapter. Broker order-management updates and market-data streams are separate inputs. Use bracket orders for a supported long equity entry with protective stop and target legs, validating current broker constraints before implementing the integration.

Persist an order intent with a unique deterministic client_order_id before submission. Its identity includes strategy version, symbol, session and entry attempt. A database uniqueness constraint and a single authorised execution worker prevent independent duplicate submissions.

After a timeout, the outcome is UNKNOWN. Query the broker by client_order_id; do not immediately retry with a new ID. If the broker cannot establish the outcome, pause that symbol and reconcile. A client ID alone is not proof of exactly-once execution: the broker and database do not share a transaction.

Track intent, submitted, acknowledged, partially filled, filled, cancelled, rejected and unknown states. Deduplicate broker events and validate transitions. A partial fill is already exposure. Check protective-order state for filled quantities; do not assume acceptance of a parent means every share is protected.

At startup, disable new entries until account, open orders, positions, daily attempts and persisted kill state reconcile. Unexpected positions are surfaced and paused rather than automatically treated as ours. End-of-session recovery has a deadline and escalates unresolved exposure visibly.

## 8 Backtesting and research

Historical bars pass through the same domain event handler and strategy used in paper mode. The simulator replaces only clock, broker and fill behaviour. Shared code reduces logic drift but does not make simulated execution identical to the market.

Use chronological replay. Market-entry signals fill no earlier than the next bar open plus configured adverse slippage. Stops gapping through their level fill at the adverse open; ordinary stop crossings fill at the stop plus adverse slippage. Targets require price contact under a documented limit-fill assumption. When a bar touches both stop and target and ordering is unknown, assume stop first and count ambiguous bars. Finer data is a later improvement.

Apply fees and slippage as configuration; run sensitivity scenarios rather than assuming zero costs. Do not fill a protective exit before the entry exists. Mark positions to market every bar and include equity changes, rather than reporting only closed-trade returns.

Report trade count, net P&L, exposure, win rate, average win and loss, expectancy, profit factor, maximum drawdown and daily returns. Sharpe estimates need a declared return frequency and annualisation rule; short samples and few trades should remain visibly limited.

Keep a chronological held-out period. Choose parameters on training/validation data and evaluate the held-out period once. Record every investigated configuration to expose selection bias. Compare against buy-and-hold and no-trade references using consistent capital and dates. Later walk-forward analysis evaluates parameter stability. Trade-sequence Monte Carlo can illustrate path sensitivity but cannot prove a stable future edge.

## 9 Persistence and observability

PostgreSQL tables: runs, dataset_manifests, strategy_configs, bars or dataset references, signals, risk_decisions, order_intents, broker_events, fills, positions, equity_snapshots and control_events. Use migrations and foreign keys. Persist immutable configuration snapshots and code commit IDs for every run.

Signals rejected by risk must still be stored with reasons. Order intents have a unique client ID; fills have unique broker execution identities where available. Broker events preserve event and ingestion timestamps. Prefer normalised tables plus limited JSON payloads for provider-specific data.

Structured logs include run ID, symbol, session, signal ID, client ID and decision reason. Never log credentials. Health checks distinguish process alive, stream connected, data fresh, reconciled and entries enabled. A running process can still be unable to trade safely.

## 10 Repository and implementation choices

Suggested files: pyproject.toml; README.md; .env.example; src/orb/domain.py; src/orb/config.py; src/orb/session.py; src/orb/strategy.py; src/orb/risk.py; src/orb/engine.py; src/orb/backtest.py; src/orb/adapters/alpaca.py; src/orb/storage/; tests/; fixtures/; docs/decisions/; migrations/.

Use a src layout to avoid accidentally importing the source tree instead of the installed package. Use dataclasses and enums for domain types, a small Protocol for broker and clock interfaces, pytest for behavioural tests, and a formatter/linter. Pin a supported Python version and record a dependency lock file. Validate current SDK compatibility when adding Alpaca dependencies.

Use GitHub Actions for deterministic tests without secrets. Broker integration tests are separate and opt-in. Keep API keys in environment variables, exclude .env from Git, and provide placeholders only. Docker Compose is added when PostgreSQL integration exists; it is not required to understand the first strategy tests.

## 11 Six milestones and acceptance criteria

| Milestone | Deliverable | Evidence required |
|---|---|---|
| 1 Foundations | Domain models, session rules, configuration, ORB calculation | Known bars produce expected range; boundaries and invalid data tested |
| 2 Historical replay | Loader, replay clock, next-bar fills and trade ledger | Hand-calculated scenario matches; no same-bar entry leakage |
| 3 Risk and persistence | Sizing, gates, PostgreSQL schema and order intents | Limit failures reject; duplicate intent rejected; restart state recoverable |
| 4 Paper integration | Streaming adapter, bracket submission, update handling | Paper order lifecycle captured; disconnect and unknown submission exercised |
| 5 Reliability and reporting | Reconciliation, kill switch, metrics, CI | Recovery scenario passes; flatten verified; repeat runs reproducible |
| 6 Portfolio evidence | README, architecture notes, demo, limitations | Fresh install works; actual results and implementation scope documented |

Test strategy behaviour at exactly 09:45 and 11:00, daylight-saving transitions, holidays, early closes, incomplete range, duplicate bars, out-of-order input, zero-width range, invalid prices and repeat breakout bars. Test risk with tiny stop distance, zero affordable size, reserved exposure and daily-loss boundaries. Test recovery with timeout after broker acceptance and partial fill during cancellation.


## 12 Official integration references

Current broker-specific details must be checked when implementing the adapter. References checked on 10 October 2026:

- Order submission and client IDs: https://docs.alpaca.markets/us/docs/working-with-orders
- Query by client order ID: https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Order update stream: https://docs.alpaca.markets/us/docs/websocket-streaming
- Market-data stream: https://docs.alpaca.markets/us/docs/streaming-market-data

The strategy parameters, architecture and proposed operational defaults in this document are project design decisions. They are not broker guarantees or evidence of profitability.
