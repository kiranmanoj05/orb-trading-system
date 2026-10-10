# Opening Range Breakout Trading System Project Report

Living project documentation • Kiran Manoj • Started 10 October 2026

## Project status

Current stage: first offline implementation. Validated market bars and opening-range calculation are implemented. Historical performance, breakout detection, paper execution and live execution remain unverified. Intended future behaviour is distinguished from completed functionality.

The companion ORB Trading System Specification defines detailed rules and acceptance criteria. This document explains the motivation, design journey, evidence and limitations. Changes to strategy rules must update both documents. Version control should preserve the history of meaningful changes.

## Motivation

This project explores how to turn a trading idea into a reliable software system. A price crossing a threshold is simple; making the resulting decision reproducible, controlling exposure, handling uncertain order outcomes and recovering after failure are substantially harder.

The aim is to build practical experience with event-driven Python, financial time-series data, external APIs, relational persistence and testing. A second aim is to evaluate a clear strategy without confusing a successful implementation with a profitable investment method.

For internship interviews, the strongest outcome is a demonstrable system whose decisions I understand: why a trade candidate existed, why risk accepted or rejected it, what the broker actually did, and what happened when part of the system failed. These motivations should be refined in Kiran's own words as development progresses.

## Why Opening Range Breakout

ORB uses the highest and lowest prices in an initial session window as reference levels. Our first version asks whether a completed later bar closes above the initial range high.

The hypothesis is that a move above the opening range may sometimes indicate continued directional activity. This is a hypothesis to test, not an established advantage for our implementation. Breakouts can fail, transaction costs can remove apparent returns, and behaviour can vary by market regime.

ORB is a useful engineering starting point because its rules can be expressed precisely. We can hand-calculate example outcomes, test session boundaries and inspect each signal. Its small initial parameter set also makes it easier to see which changes affect results. A more complicated strategy would make it harder to distinguish implementation bugs from research choices.

The 15-minute range, midpoint stop and 2R target are baseline choices from the specification. They have not been selected through demonstrated research. Keep them fixed for the first implementation; investigate alternatives later using a declared validation process. Record why any baseline rule changes instead of replacing it silently after observing poor results.

## Intended use and end goals

The first usable product is an offline research tool: ingest known data, reproduce signals, simulate execution and export an auditable report. The next is an Alpaca paper-trading application that exercises the same decision logic on streamed bars.

Potential users include the project author, a reviewer running a reproducible demonstration, and a researcher comparing strategy configurations. A small status interface can later expose health, positions, risk state and the reasons for recent decisions.

Real-money use is a separate future stage. It requires verified broker/account eligibility, operational monitoring, failure recovery, a documented execution model and an explicit decision to enable it. Paper fills and backtest results do not establish live suitability. The MVP should remain useful even if the strategy has no profitable edge: reliable replay, transparent decisions and negative research results are valid outcomes.

## Initial strategy contract

| Decision | Initial rule |
|---|---|
| Market | US equities ETFs SPY and QQQ |
| Direction | Long only |
| Input | Completed one-minute OHLCV bars |
| Opening range | 09:30 inclusive to 09:45 exclusive in America/New_York |
| Candidate | Eligible completed bar closes strictly above frozen range high |
| Entry timing | After the signal bar completes; never assumed filled at that same close |
| Cutoff | Candidate must be available before 11:00 New York time |
| Stop | Opening-range midpoint |
| Target | Planned entry plus twice planned entry-to-stop distance |
| Planned risk | 0.5% equity per trade, subject to additional size and exposure limits |
| Attempts | One entry attempt per symbol per session |
| End of session | Begin flattening five minutes before actual exchange close |

The 09:44 bar is part of the range and becomes available at 09:45. The 09:45 bar is the earliest eligible breakout bar and becomes available at 09:46. This timestamp distinction prevents using information before it was available.

Skip an incomplete opening range. Honour exchange holidays and early closes. Missing bars, unknown positions and uncertain submissions should produce explicit outcomes, not silent guesses. Proposed operational defaults in the specification remain provisional until implemented and tested.

## Technology choices

| Technology | Intended responsibility | Rationale and trade-off |
|---|---|---|
| Python | Strategy, replay, risk and orchestration | Fits the existing data-analysis workflow; requires disciplined types and tests |
| Dataclasses and enums | Domain contracts and lifecycle states | Lightweight and readable; validation must still be explicit |
| Decimal | Domain money and prices | Makes monetary rounding deliberate; slower than array arithmetic |
| NumPy and pandas | Research and reporting | Convenient time-series analysis; avoid placing a second strategy implementation in notebooks |
| Alpaca API | Paper broker integration | Concrete external execution target; provider-specific behaviour stays in an adapter |
| WebSockets | Incoming market and order events | Supports streaming; disconnects, duplicates and ordering require handling |
| PostgreSQL | Decisions, intents, fills and control history | Constraints and transactions support an audit trail; adds deployment and migration work |
| pytest | Behavioural verification | Tests session rules and failure scenarios using known inputs |
| Git and GitHub Actions | History and repeatable checks | Makes changes reviewable; ordinary CI should not require broker secrets |
| FastAPI later | Status and control endpoints | Useful once the engine exists; unnecessary for the first calculation |
| Docker Compose later | Repeatable application and database setup | Helps integration and demonstrations; adds setup complexity |

Exact dependencies and supported versions will be selected and locked during implementation. This table is an intended stack, not a list of installed or completed components.

## Architecture

Market data is normalised into domain bars. The session service determines which bars belong to the range. The strategy emits candidates. Risk checks produce approved or rejected decisions. Approved decisions become persisted order intents before the execution adapter contacts the broker. Broker events then update order and position state. Reporting reads the resulting evidence.

Backtesting substitutes a replay clock and simulated broker. Paper mode substitutes streamed input and the Alpaca adapter. Both call the same strategy and risk logic. Sharing decision code reduces divergence but does not remove differences in latency, fills, feed coverage or corrected historical data.

Start with one process and clear module boundaries. There is no demonstrated scaling need for microservices. Introduce asynchronous queues when streaming arrives, with explicit backpressure and overflow handling. Database transactions can protect local reservations; they cannot make a broker request and local write one atomic operation.

## Important design decisions

| Decision | Reason | Limitation or revisit condition |
|---|---|---|
| Completed bars | Stable, explainable decision points | Reacts later than tick-based signals |
| Broker-independent strategy | Offline tests and repeatable replay | Execution differences still need separate modelling |
| Long-only baseline | Smaller initial state space | Cannot assess short breakout behaviour |
| Next-bar simulated entry | Avoids impossible same-close fills | Still approximates post-signal execution |
| Conservative ambiguous exits | Avoids optimistic ordering assumptions | Can understate outcomes; finer data may resolve ambiguity |
| Persist intent before submission | Makes uncertain outcomes recoverable | Requires reconciliation after crashes and timeouts |
| Deterministic client order ID | Links retries and broker queries to one intent | Does not independently guarantee exactly-once execution |
| Fail closed on uncertain state | Prevents new exposure while facts are unknown | Reduces availability and requires clear recovery controls |

These decisions become individual architecture records when the relevant implementation exists. Each record should contain context, alternatives, decision, consequences and a condition for reconsideration.

## Risk and reliability

Sizing uses the planned stop distance and account limits. Pending entries reserve exposure so concurrent candidates cannot each spend the same available capacity. A strategy signal is not permission to bypass account controls.

An accepted order is not a fill. A partially filled order already creates exposure. A submission timeout is an unknown outcome, not evidence of rejection. Recovery queries the existing intent before deciding whether another action is safe.

The kill controls distinguish pausing new entries from cancelling entry orders and flattening positions. Closing risk must remain possible while entries are disabled. Restart reconciliation must compare local and broker state before trading resumes. Flattening is complete only when the broker state confirms the intended outcome.

Stops and loss gates are operational controls, not guarantees of maximum loss. Gaps, adverse fills and failures can exceed planned limits. Record breaches and actual execution rather than treating configured limits as proven behaviour.

## Evaluation and evidence

Software correctness is evaluated with known examples, boundary tests, invariants and failure scenarios. Research quality is evaluated separately using chronological data splits, declared costs, reproducible configurations and held-out results.

Every run should identify code version, configuration, dataset provenance and checksum, fill assumptions and output location. Reports should include trades, equity, drawdown, costs, exposure and rejected decisions. Parameter experiments must be logged, including unsuccessful ones, to make selection bias visible.

Paper validation examines event handling, broker state, reconciliation and controls. It does not validate the realism of historical fills or prove future returns. Compare planned and actual execution values rather than merging simulated and paper results into one performance claim.

| Claim | Evidence needed | Current state |
|---|---|---|
| Range calculation follows the contract | Fixture and boundary tests | Planned |
| Backtest avoids same-bar entry leakage | Hand-calculated replay | Planned |
| Shared strategy logic works in both modes | Replay of identical events plus integration evidence | Planned |
| Duplicate submission is handled | Timeout and duplicate-event scenarios | Planned |
| Restart recovery works | Controlled interruption and reconciliation exercise | Planned |
| Paper execution works | Captured paper order lifecycle | Planned |
| Strategy has a measured edge | Held-out results with costs and uncertainty | Unestablished |

## Limitations to investigate

- Minute OHLCV bars do not reveal the order of intrabar events or executable bid and ask prices.
- Slippage, spreads and liquidity are approximated in the first simulator.
- Historical corrections and different feeds can change an opening range.
- SPY and QQQ share market exposure; two positions are not necessarily independent risk.
- Parameter search, short samples and favourable date selection can create misleading results.
- Paper execution cannot reproduce every real-market behaviour.
- Network outages and non-atomic broker/database operations create uncertain states.
- The current design has no verified implementation or performance evidence yet.

Each limitation should eventually link to a test, measurement, documented assumption or deliberately deferred improvement.

## Development sequence

1. Build domain models, session boundaries and the opening-range calculation with deterministic fixtures.
2. Add breakout candidates and a replay loop, then a documented simulator and trade ledger.
3. Add sizing, risk reservations and persistent intent with PostgreSQL migrations.
4. Add Alpaca paper data and execution adapters, including order updates.
5. Exercise disconnects, submission uncertainty, partial fills, restart and kill controls; add reports.
6. Prepare a reproducible demonstration and revise documentation and CV wording around actual evidence.

Do not couple progress to profitability. If the baseline performs poorly, preserve the result and analyse it before changing the rules.

## First development session

Create a small installable repository with src/orb/domain.py, src/orb/session.py, src/orb/strategy.py, tests and a dependency configuration. Implement a validated immutable Bar and an OpeningRange result. Keep broker credentials and database setup out of this increment because neither is needed for the calculation.

Construct 15 consecutive fixture bars with a highest high of 100 and lowest low of 98. Assert the midpoint is 99. Verify that a 09:45 bar is excluded from range construction, a missing minute causes rejection, and naive timestamps are rejected. These tests verify financially meaningful boundaries rather than merely repeating implementation statements.

Next, add a candidate from the first eligible closed breakout bar and test the cutoff. Once this works, the code can be explained line by line and replayed without relying on any external service.

## Learning and interview notes

For every increment, record the problem, implementation, reasons, alternatives, test evidence and remaining limitations. Kiran should be able to trace a fixture through the code and predict its output before running it.

Useful interview topics include separating signals from orders, modelling time correctly, preventing look-ahead bias, why idempotency needs persistent state, why partial fills matter and why shared code does not guarantee identical historical and paper execution.

Suggested explanation once implemented: “I started with a precise strategy contract and deterministic tests, then separated decisions from execution. That let me reuse the strategy for historical replay and paper trading. I explicitly modelled uncertain submissions and reconciled broker state rather than treating a timeout as a failed order.” Only use past tense after those features are complete.

Record AI assistance honestly. The project story should show ownership through design judgement, debugging, verification and an ability to explain the implementation. Demonstrated understanding matters more than claiming every line was written unaided.

## How this document will evolve

After each milestone, update status, completed components, verification evidence, decisions, limitations and the next step. Link to repository commits, tests and reports when they exist. Keep the detailed specification authoritative for behaviour and use decision records to explain changes.

The final version should read as a complete account: initial motivation, strategy hypothesis, architecture, difficult problems, choices made, experiments, results, lessons and further work. Preserve negative findings and unresolved limitations. Remove provisional claims or turn them into clearly dated decisions; do not retrofit the motivation to make results appear inevitable.

### Milestone record template

- Date and commit:
- Problem addressed:
- Behaviour implemented:
- Decision and alternatives:
- Verification and results:
- Limitation discovered:
- What Kiran can now explain:
- Next step:

### Change log

10 October 2026: Created the project narrative, intended stack, design rationale, evidence register and development sequence. Implementation remains pending.


## First implementation update

10 October 2026: Added an immutable validated Bar, OpeningRange result and pure range calculation, with 19 passing tests covering incomplete windows, duplicate minutes, availability time, UTC input, daylight saving and invalid prices. Language decision is recorded in 0001-language-choice.md. The range calculator still assumes its caller supplies a valid exchange session; a calendar service is pending.

Verification: python -m pytest -q completed with 19 passed on Python 3.12. Python 3.11 CI is configured but has not yet run on GitHub.
