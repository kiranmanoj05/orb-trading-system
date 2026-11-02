# Paper execution v1

This adds a **paper-only**, local background worker. Historical backtests are
separate from broker activity. The worker cannot select a live broker URL.
The account balance is Alpaca paper equity; it does not include backtest P&L.

## Start

Update `main`, install `.[dev,dashboard]`, run `npm --prefix web ci` and
`npm --prefix web run build`, then `python -m orb.dashboard_api` in the terminal
that has your **paper** credentials. Open http://127.0.0.1:8765.

In **Settings → Paper trading**, choose **Paper Balanced**, select a feed and
click **Start paper trading**. IEX is the initial choice; SIP needs current
real-time data access. Feed choice changes the source of signals. A delayed
feed fails freshness checks. Do not paste keys into the browser or chat.

The worker polls every 15 seconds while the server runs. Keep the Mac awake.
The browser may be closed. A server restart requires explicitly starting the
worker again; saved attempts prevent repeat entry, and outstanding attempts
are reconciled before any new admission. Do not delete the paper database.

| Starter | Allocation ceiling | Planned risk ceiling | Position allocation cap | Target | Daily loss entry block |
|---|---:|---:|---:|---:|---:|
| Paper Calm | $100,000 | 0.25% ($250 at full allocation) | 50% | 2R | 1% |
| Paper Balanced | $100,000 | 0.5% ($500 at full allocation) | 75% | 2R | 1.5% |
| Paper Active | $100,000 | 1% ($1,000 at full allocation) | 100% | 2R | 2% |

These are **unvalidated operational experiments**, not optimised strategies.
Risk ceilings use the smaller of allocated capital, broker equity and cash;
whole-share sizing, allocation and aggregate-risk caps can make deployed risk
much smaller. Stop execution can gap; planned risk is not a guaranteed loss cap.
Built-ins are immutable. Duplicate/edit/save a version to customise it. Start
freezes that exact record; later preset edits do not change the running worker.

## Supported strategy and execution

- SPY long only, eligible weekdays from the selected preset, **one attempt per
  NY session and one position**. Other entry/concurrency caps are rejected.
- Opening range 09:30–09:45 New York; first completed consecutive-minute close
  above its high before 11:00 NY. Midpoint stop; configured target R.
- Only a fresh first breakout is admitted. Starting after an earlier signal
  does not buy retrospectively. Missing minutes pause eligibility.
- Fresh quotes (rechecked against elapsed request time before submission), spread ≤10 bps; buy limit at ask plus 5 bps rounded up to cents.
  Quotes over one quarter of initial stop distance beyond the signal close are
  skipped. No leverage. External positions or open orders block entry.
- Broker-held **GTC bracket** with a sell stop and take-profit limit. An unfilled
  entry older than 60 seconds or a partial entry is canceled; cancellation must
  be confirmed before residual whole shares are closed. Alpaca activates bracket
  exits after full entry fill; partial-entry management depends on this worker.
- Five minutes before the calendar's regular close: request cancellation of
  owned bracket orders, wait for all terminal statuses, re-read actual position
  and open orders, then submit only the reconciled residual closing quantity.
- `Stop new entries` keeps management running. Server shutdown leaves existing
  broker orders intact, but cannot guarantee intraday flatten or partial-fill
  handling while offline. A rejected/expired closing order or an uncertain POST
  requires inspection; the worker never automatically resends it.
- Incomplete/missing bracket-leg evidence never proves a safe close. Duplicate
  leg identities block fill accounting. Lost exit protection triggers cancellation
  and reconciliation before residual closing. Rejected, canceled-partial or changed
  closing orders require manual inspection and are never blindly resent.
- Durable intent **before** submission; deterministic client identity across
  presets/restarts; lookup after uncertain response, never blind retry. Account
  identity bound to the database. File lock excludes competing workers sharing
  this state path. This is a bounded broker-authoritative adapter, not integration
  of the more general offline coordinator/reconciliation ledgers.
- Daily-loss anchor is equity at the first eligible worker observation that day;
  account changes before that observation are not measured. The dollar budget can
  tighten but never widen during that NY session. A breach latches durably through
  equity recovery, restart and preset changes; position management continues and
  a later NY session gets a distinct anchor/block. With one attempt/day
  it is a new-entry block, not a mechanism to liquidate positions or stop losses.

## Clear chart modes

Paper mode is the default. It shows only the worker's current-session candles
and broker-confirmed cumulative-average fill markers. Without current-session
data it shows an empty state; it never falls back to an old simulation. No
opening-range, planned-entry, stop or target overlays are drawn in paper mode.
Pending orders are available in Trades but are not depicted as fills.
Broker observations include nested bracket legs; duplicates are removed.
Partial fills without a broker `filled_at` are listed without inventing a fill
timestamp. Observations and chart polls have independent timestamps and are
not atomic. The latest 500 parent orders may not be a complete account history.

Simulation mode retains real imported historical runs and clearly labelled
simulated trades. Saved synthetic runs are filtered from the workspace without
removing test fixtures or deleting stored evidence. Backtest returns never
change the Alpaca account balance.

## Verification and remaining work

Automated broker tests use injected fixtures and submit no orders. No live
account credentials or Mac runtime are available in the development environment.
A real paper session is still needed to validate entitlement, real broker order
shapes, partial/cancel timing and restart operation. This does not establish
profitability. Polling is the initial transport; streaming order events and a
full immutable broker execution/P&L ledger remain future work.

Alpaca references: [order lifecycle and brackets](https://docs.alpaca.markets/us/docs/orders-at-alpaca),
[client order lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid).
