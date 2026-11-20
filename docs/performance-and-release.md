# Performance reporting and release acceptance

ORB is an ETF research and bounded Alpaca paper-execution project. It is not a
futures bot, a demonstrated profitable strategy, or a service that runs when its
local server is shut down. The approved minimal dark workspace is unchanged.

## What this release adds

In Simulation, open **Details → Performance report → Calculate report**.
Download the readable Markdown report or its complete JSON. The API reads an
integrity-checked, saved engine run; it never accepts uploaded profit figures.
Missing regular-session data, uncertain trades or missing marks withhold
aggregate statistics and passive benchmark returns.

The report includes modelled net P&L, per-trade expectancy, win fraction, profit
factor, return, sampled drawdown, daily P&L, and independent SPY/QQQ price-only
buy-and-hold benchmarks. It retains the source/configuration/engine identities.
No losing trades means profit factor is undefined, not infinite or a win claim.
An empty closed-trade sample has undefined expectancy and win rate.

The 95% descriptive interval resamples **consecutive five-session blocks**, with
2,000 circular moving-block resamples and seed 1729. Zero-trade complete sessions
remain in the sample. At least 20 sessions and four blocks are required. The
interval describes mean historical session dollars with fixed historical sizing;
it does not rerun the account along resampled paths, correct parameter-selection
bias, model regime changes, or predict the chance of future profit. Twenty
sessions is an implementation threshold, not a sufficiency claim.

Passive benchmarks use independent full-capital accounts, whole shares, the
first regular-session open and final regular-session close, and the same
per-side fee/slippage model. They exclude dividends and do not match ORB's
exposure, risk or intraday holding period. They are not a combined portfolio.
The cash comparator assumes zero interest/costs. All return values in JSON and
the report's tables are fractions, not percentages.

For larger canonical CSVs, recompute the report directly:

```bash
python -m orb performance data/research/bars.csv \
  --start-session 2026-10-01 --end-session 2026-10-30 \
  --slippage-bps 2 --fee-per-share 0.005 \
  --data-label "Alpaca historical research; user-supplied archive" \
  --output results/performance.json --report results/performance.md
```

The dates and costs above are **examples**, not provider prices or a statement
that this dataset exists. Both SPY and QQQ are required across declared sessions
by the current research baseline. Do not shorten bounds or omit a missing symbol
to manufacture eligibility. The CLI accepts canonical CSV, not arbitrary report
JSON. Use `--synthetic` for invented data. Dataset labels alone do not authenticate
a provider. Sources outside the declared dates fail; output cannot overwrite the
input or collide with the companion report. The report is limited to 2,000
sessions at default resampling settings.

## Paper costs and profit

In **Trades → Paper cost estimates → Calculate report**, the separate paper
report reads saved owned fills and their immutable attempt presets. It makes no
broker request and changes no database state. A per-trade estimate requires:

- Terminal order evidence and matched entry/exit fill activities.
- A finished activity scan, no truncated displayed activity list, and agreement
  of each order's quantity and exact notional with its cumulative snapshot.
- Each filled order's last activity time matching its reported terminal fill time.
- All entry fills no later than exits, and no outstanding correction hold.

The formula is:

`estimated net = observed fill gross − modelled per-share fees − additional bps cost stress`

The per-share fee applies to every entry and exit fill. Extra bps apply to the
absolute filled notional on both sides. This is a sensitivity haircut on already
observed prices: it can overlap spread or slippage already reflected in those
fills and is **not measured slippage** or a new order execution simulation.
It does not rerun sizing or targets. Zero preset costs produce a zero-cost
diagnostic, not complete live cost accounting.

Account FEE records remain separate: a date or account amount alone does not
establish trade attribution or when cash was booked. Missing fees never become
verified zero. Broker trade fees, broker net profit, fee-completeness and account
return stay unavailable. The UI and JSON distinguish these from estimated net.
The report includes preset/evidence hashes so an estimate can be reproduced.
The displayed paper report is invalidated when its underlying evidence changes;
an older in-flight response cannot restore it after that invalidation.

Totals require every saved attempt to be estimable and the attempt list to be
untruncated. An unobserved, open, inconsistent or held attempt withholds the
entire total. Confirmed no-entry attempts contribute zero only with matching
order and activity evidence. Detailed unavailable reasons remain visible.

## Recovery acceptance

Existing native behaviour is intentionally retained:

- A durable intent and risk reservation precede entry submission.
- Unknown submissions and management commands are not automatically replayed.
- Restart requires explicit Start and fresh agreeing broker inventory evidence.
- Pending/partial attempts retain headroom. Terminal release needs matching
  evidence; cancellation receipts are not fill/cancellation confirmations.
- Corrections remain blocked. Reading performance, a matching cash diagnostic,
  or a review record cannot clear a hold or authorise a retry.

**General correction clearance and complete account/settlement reconciliation
are still unfinished.** Do not delete the database, reset an anchor, or edit
SQLite to make Start available. Preserve it, inspect the broker, and retain the
discrepancy. This release does not claim unattended recovery is complete.

## First supervised account session

Update the repository, install `.[dev,dashboard]`, build the frontend, then start
`python -m orb.dashboard_api` in the terminal with paper credentials available.
Never send keys to chat or include them in portfolio screenshots.

1. Open `http://127.0.0.1:8765` and refresh the paper account. Confirm the account,
   positions and open orders against Alpaca.
2. In Settings → Paper trading, select an exact saved preset and entitled feed.
   Start **before 09:45 New York** so the first possible fresh breakout can be
   observed. The worker never invents a missed entry. A skip/no-trade is valid.
3. Keep the server running and Mac awake. Stop new entries leaves management
   running; shutting down the server does not.
4. When a signal occurs, compare the parent order, filled quantity and protective
   legs with Alpaca. Inspect pending orders in Trades; markers require fills.
5. Observe exit/flat inventory, then refresh saved evidence and fill activities.
   Export evidence and calculate the conditional paper cost report.
6. Separately rehearse restart with injected responses before using it around an
   actual open paper position. Preserve uncertain outcomes; never force a retry.

Use New York exchange time and confirm the broker clock/calendar at runtime.
London offsets change around daylight-saving transitions; do not reuse a dated
local-time conversion. A halt or account restriction can still prevent a trade.

## Portfolio acceptance and remaining evidence

The following are separate claims. A green unit suite cannot substitute for any
row's evidence.

| Claim | Required evidence | Current boundary |
| --- | --- | --- |
| Research/software works | Source recomputation, integrity gates, cost arithmetic, build and tests | Automated verification possible locally |
| Paper execution works on the user's account | Broker-confirmed signal/order/fill/protection/exit lifecycle | Requires an actual session and local credentials |
| Recovery works against the provider | Controlled restart/disconnect observation with exact order and inventory agreement | Injected tests exist; actual account acceptance remains |
| Net profit is measured | Complete attributable fills and costs, cash adjustments separated | Conditional cost model available; broker net unavailable |
| Strategy has an edge | Broad history, declared trials, untouched testing, robustness and prospective evidence | Not established |

Research and paper do not yet implement identical execution: the former can
include SPY/QQQ and next-minute idealised fills; the latter is SPY-only with fresh
quotes, marketable limits and broker brackets. An aggregate research report is
not validation of the paper worker. The separate [SPY paper-preset study](spy-paper-preset-study.md)
now matches instrument scope and an exact saved paper configuration, while
explicitly retaining candle-fill versus quote/limit execution differences.
Execution realism and prospective evidence remain required before a strategy-performance claim.

For portfolio publication now, use the accurate case study in
[portfolio-case-study.md](portfolio-case-study.md). Add numeric market results
only after running a real archive. Synthetic verification belongs in engineering
evidence and must never be shown as investment performance.

The study now retains four prespecified cells: saved costs, additional costs,
one-minute delayed entry, and delayed entry plus an assumed 4 bps full spread.
Each reruns the engine, and only eligible pairs receive deltas. Spread is added
to saved slippage; it is not measured from OHLCV. Full fills, limit-target touches
and no pending reservation during the hypothetical delay remain assumptions.
See the study's explicit execution contract before interpreting comparisons.
