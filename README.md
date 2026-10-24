# ORB Trading System

A Python research and paper-trading project implementing Opening Range Breakout.

## Status

The implementation validates completed one-minute bars, calculates a 15-minute
opening range and emits at most one long breakout candidate per symbol/session.
An offline CSV replay records candidates and per-session outcomes chronologically.
A fixed-share execution simulator records next-minute fills, exits and net P&L.
A pure cost-aware sizing calculator and sequential shared account ledger are
connected through chronological shared-account portfolio replay.
Broker integration and paper trading are still planned.
An independent SQLite entry-intent journal now provides atomic local claims,
restart evidence and cumulative fill observations for future paper integration.
An offline durable coordinator connects admitted/rejected candidates, modeled
account reservations and the session-loss latch to that journal atomically.
An independent durable reconciliation ledger reconstructs cumulative partial
entry and exit fills and requires an exact complete broker snapshot before its
recovery gate can clear.
A bounded Alpaca paper adapter now performs only fixed account, position and
open-order GETs, requires two equal normalised passes and feeds that gate while
leaving execution disabled.
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
python -m orb account-demo
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
`unresolved`, with no invented exit or P&L. This command is not yet connected
to the standalone sizing calculator or shared account ledger.

This model assumes instantaneous next-open execution and immediate bracket
activation after entry. It has no spread, queue, latency, volume capacity,
partial fills or broker fee schedule. Prices are unrounded Decimal values;
provider adjustments and tradable tick rules require later adapter work.
Multi-symbol positions are independent fixed-share scenarios, not a portfolio.

## Account snapshot sizing

```bash
python -m orb size --equity 10000 --cash 10000 --entry 100.30 --stop 99
```

This hypothetical snapshot returns 24 shares, limited by the default 25%
single-position notional cap. Defaults also allow 0.5% equity planned stop risk
and 100% aggregate gross notional exposure. These are research parameters,
not recommended settings. `--risk-fraction`, `--max-position-fraction`,
`--max-gross-fraction` and `--gross-exposure` make them explicit.
`--slippage-bps` and `--fee-per-share` include adverse entry/stop slippage and
round-trip fees in planned risk; cash capacity includes the entry fee.

`size_long` floors to whole shares and returns the binding constraint and
planned amounts. Zero capacity reports `below_one_share:<constraint>`. All
fractions must be positive and at most one; no leverage is allowed. Entry is
an assumed pre-slippage price strictly above the stop. Exposure is supplied
as aggregate entry notional, with accepted orders included by the caller.

This is a pure calculator, separate from `simulate`. It does not reserve cash,
fetch account data, update equity, check snapshot freshness or submit orders.
Reusing the same snapshot for simultaneous candidates can overallocate funds.
The standalone account ledger below owns reservations and releases; historical
replay integration is provided by `portfolio` below.
Stop gaps can exceed planned risk; this is not a maximum-loss guarantee.
Position and gross caps do not measure SPY/QQQ correlation or volatility.

## Shared account ledger

`AccountLedger` sizes and reserves from its current snapshot in one sequential
operation. Pending orders hold cash (including entry fees) and gross entry
notional. A full fill spends cash, releases unused reservation and records an
open position. Cancellation releases only a pending order. A full exit credits
proceeds less its fee and records net realised P&L including both side fees.
Unknown positions retain allocation until an explicit exit resolves them.

The ledger rejects reused IDs and multiple active allocations for one symbol.
A fill above reserved notional/cash is rejected without releasing funds;
resizing/cancellation must be explicit. Fill prices are already slipped, so
costs are not applied twice. Failed transitions leave state unchanged.

`python -m orb account-demo` traces a synthetic lifecycle with initial cash
10000: reserve 24 SPY and 24 QQQ shares at 100.30, leaving 5185.60 available;
fill SPY, cancel QQQ, then exit SPY at 102.90. Final cash is 10062.40 and net
realised P&L is 62.40. This demonstrates accounting, not historical performance.

Equity carries positions at entry cost and deducts paid entry fees; it is not
marked to market. Gross exposure also uses entry notional. Limits are admission
checks, not continuously enforced market-value caps. There is no aggregate
planned-stop-risk or daily-loss cap yet. Full fills only; no partial fills,
event timestamps, settlement delays, persistence, broker reconciliation or
concurrent-worker protection. Callers must serialize operations and supply
chronologically valid events. An insolvent account cannot produce a valid
sizing snapshot. `simulate` remains an independent fixed-share scenario.


## Chronological portfolio replay

```bash
python -m orb portfolio fixtures/orb_execution_sample.csv --initial-cash 10000 --output results/portfolio.json
```

The synthetic fixture sizes 24 shares, fills at 100.30 and exits at 102.90.
With zero costs, final cash is 10062.40 and realised P&L is 62.40. This is
accounting evidence from invented bars, not historical strategy performance.

Options: `--risk-fraction`, `--max-position-fraction`, `--max-gross-fraction`,
`--slippage-bps` and `--fee-per-share` use the same sizing/cost contracts.
The account begins entirely in cash. Sizing and immediate full reservation/fill
occur at the executable next open, using that open and the current account.
Quantity-independent fill paths reuse `simulate`; future outcomes never enter
the admission snapshot. This depends on no volume/market-impact modelling.

Events are applied chronologically. Existing exits at a timestamp precede
entries, and simultaneous entries sort alphabetically by symbol then signal ID.
Intrabar exits release resources only at minute completion. A new position's
same-open exit follows all entries at that timestamp. Equal-time CSV row order
does not affect results; globally out-of-order input is still rejected.
The tie convention assumes instantaneous access to proceeds, not settlement.

JSON includes all candidates (including unfilled/rejected), sizing decisions,
accepted entry/exit snapshots and a final ledger. Missing/truncated positions
retain entry-cost exposure across later sessions; another candidate in the same
symbol is rejected. Unknown positions are labelled unresolved at evaluation end.
Rejected trades contain no hypothetical fills or P&L.

The ledger still exports explicitly labelled carrying equity/exposure at entry
cost. Admission now uses separate fresh marked snapshots and an aggregate
planned-stop-risk cap and session loss latch (see below). Latency, partial fills,
durable state and broker orders remain planned. Full-input and snapshot storage
are suitable for small research fixtures, not large production streams.

## Reproducible evaluation

```bash
python -m orb evaluate fixtures/orb_execution_sample.csv \
  --data-label synthetic-fixture \
  --output results/evaluation.json --report results/evaluation.md
```

Uses the same baseline strategy, fills and shared account as `portfolio`.
Every candidate remains visible as closed, unresolved, unfilled or rejected.
Net P&L and win fraction are explicitly the **closed subset**, never a claim
about unknown exits. Total return and minute-sampled drawdown require complete
coverage and resolved outcomes. The fixture's realised 62.40 is synthetic
accounting evidence, not observed historical performance.

Coverage expects both SPY and QQQ on every exchange session within the date
scope, including all regular-session minutes and early closes. An entirely
absent symbol/day is missing; a missing minute is incomplete even if a trade
already closed. This strict data audit is separate from strategy outcomes.
Without date flags, scope is only the observed New York date span. To audit
omitted leading/trailing sessions, provide **both** `--start-session YYYY-MM-DD`
and `--end-session YYYY-MM-DD`. Bars outside those bounds are rejected.
Missing data beyond the declared scope cannot be inferred.

The evaluation ID hashes canonical UTC bars, exact normalised decimal values,
volume, configuration, scope, data label, shipped Python source hashes and
calendar/dependency versions. Equal-time symbol ordering, timezone spelling,
equivalent decimal spelling and file location do not change this identity.
Changing prices, volume, limits, labels, source or dependencies does.
No generated timestamp enters the report, so repeated runs in the same
environment produce identical JSON and Markdown. Python version is disclosed
separately. Preserve the input CSV, JSON manifest, dependency environment and
matching source revision to reproduce a run; a fingerprint does not archive
the data or prove provider provenance. Labels are unverified user assertions.

JSON retains the full portfolio trace and a coverage table, status/reason
counts, ambiguous exits and unresolved IDs. Markdown is its readable companion.
Neither output may overwrite input or share the same path. Results are offline;
no external dataset, news API, broker connection or orders are involved.
Next: historical-data ingestion and baseline evaluation with declared provenance.

## Completed-minute market valuation

`orb evaluate` now includes a separate `valuation` series. It samples every
regular-session minute in the declared scope, including gaps and early closes.
Post-event holdings use that minute's completed close. At the exact entry
instant the fill price is used, so a later close cannot revalue an earlier
entry. Existing exit cash takes precedence over a candle close for closed lots.
Missing held-symbol marks produce null equity/exposure/P&L; prices are never
carried forward. A later mark values an unresolved holding without inventing
an exit. Overnight-held positions are unknown at the next session open.

Complete coverage and resolved outcomes are required for total return and
maximum **minute-sampled** drawdown. These metrics are suppressed otherwise,
even if the account ends flat. Marks are not executable liquidation quotes;
unrealised net P&L includes paid entry fees but no hypothetical exit fee.
Intraminute drawdown remains unobserved. The evaluation schema is now v4.

This series remains post-event reporting; admission uses its own fresh marked
snapshots under the same completed-close timing contract.

## Fresh marked admission and aggregate stop risk

`portfolio` and `evaluate` size each new candidate from current cash, held market
value and reserved commitments. Every held symbol needs a completed close
available at that exact timestamp, or a fill already applied at that same
instant. Neither a stale close nor a future close is accepted. A missing mark
rejects the candidate as `missing_or_stale_position_mark`; it does not invent
an exit, release cash or retry the consumed daily candidate. Event evidence
records the admission snapshot, mark prices/times/sources and missing symbols.
Simultaneous entries retain alphabetical priority; earlier same-instant fills
contribute their fill price and paid fee to the next admission snapshot.

The new `--max-total-risk-fraction` defaults to `0.01` (1% of admission equity),
alongside the unchanged 0.5% per-trade default. This is a declared engineering
baseline, not a tuned parameter or safety guarantee. Whole-share sizing also
caps new initial stop risk by the remaining aggregate budget. The initial
cost-aware risk includes assumed stop-sale slippage and both entry/exit fees.
Pending orders, open holdings and unresolved holdings keep that commitment.
Cancellation or full exit releases it; a better confirmed fill can reduce it.
Marks do not release initial risk, even after an unrealised gain. This controls
initial capital-at-stop risk, not giveback from current marks or correlation.

For a pure snapshot example with 85 risk already committed and equity 10000:

```bash
python -m orb size --equity 10000 --cash 10000 --entry 100 --stop 98 \
  --committed-stop-risk 85 --max-total-risk-fraction 0.01
```

Expect seven shares and `sized:aggregate_risk_cap`: only 15 aggregate risk
capacity remains and each share risks 2. Gap losses may exceed planned risk.
These aggregate-risk checks are admission controls, not forced liquidation;
the separate daily-loss latch is described below.
Mark losses can leave an existing account above a cap; further capacity is
then zero. Standalone `AccountLedger.snapshot()` and reservations without
marks explicitly retain carrying-cost compatibility. Integrated portfolio
admission always supplies fresh marks and never falls back to entry cost.

## Session-scoped daily-loss latch

`portfolio` and `evaluate` accept `--daily-loss-fraction`, default `0.02` (2%).
This is a provisional engineering baseline, not an optimised strategy input
or guaranteed maximum loss. The reference is equity at the actual exchange
session open, fixed for that session rather than a trailing high-water mark.
When flat, known cash supplies the reference even when bars start later. The
initial replay assumes the declared cash and no pre-existing inventory.
Carried inventory without fresh opening marks makes the reference unknown;
that whole session is blocked as `daily_loss_baseline_unavailable`. Later
prices never substitute a more convenient baseline.

A dedicated clock checks every regular-session minute, including gaps and
early closes. Existing exits at a timestamp are applied before its check and
new entries. Each fill is also checked immediately, so paid entry fees can
block the next simultaneous candidate. Loss at or above the fraction of opening
equity latches `daily_loss_limit`. Recovery does not reopen admissions. Missing
held-symbol equity latches `daily_loss_equity_unavailable`, because an unseen
loss during a data gap cannot be ruled out. Missing bars while flat do not
make known cash unknown; the separate coverage audit still reports them.

`daily_loss_sessions` retains opening equity, loss limit, first blocking reason
and time, and last observation. `daily_loss_observations` retains minute checks
and after-fill checks, including missing symbols and phase. Entry traces carry
the guard state. More specific existing rejection diagnostics (active symbol,
invalid entry, missing current marks or depleted account) retain precedence.
Evaluation fingerprints the new fraction and reports blocked-session reasons.

The next actual exchange session gets a new reference and guard; weekends and
holidays do not reset it. An unresolved holding is never released by a reset.
No forced liquidation, cancellation, pending-order controller or broker kill
switch is claimed. This in-memory latch is not restart-safe. Minute checks
cannot detect intraminute dips, and gaps/fees can exceed the threshold before
observation. For example, a synthetic 31.20 realised loss from opening equity
10000 exceeds a 0.1% limit of 10 and blocks a later same-instant candidate.
Reproduce that invented two-symbol case with:

```bash
python -m orb evaluate fixtures/orb_daily_loss_sample.csv \
  --daily-loss-fraction 0.001 --data-label synthetic-daily-loss \
  --output results/daily-loss.json --report results/daily-loss.md
```

Expect one QQQ stop loss of 31.20, a rejected SPY candidate, cash 9968.80,
and `daily_loss_limit`. This short fixture is incomplete and synthetic;
aggregate performance metrics remain null, not historical evidence.

## Saved historical-response ingestion and provenance

The offline `import-alpaca` command validates a capture envelope containing an
explicit request and every saved response page. It creates a new bundle with
the exact `capture.json`, chronological UTC `bars.csv`, and `manifest.json`.
The manifest records raw/canonical SHA-256 hashes, request/feed/adjustment,
declared acquisition time, data kind, page/bar counts and a bundle identity.
There are no HTTP requests, credentials, paid services or broker orders here.

```bash
python -m orb import-alpaca fixtures/alpaca_capture_synthetic.json \
  --out-dir data/synthetic-alpaca-bundle
python -m orb evaluate-bundle data/synthetic-alpaca-bundle \
  --daily-loss-fraction 0.001 \
  --output results/bundle-evaluation.json --report results/bundle-evaluation.md
```

This explicitly invented 35-bar/two-page fixture reproduces the QQQ loss of
31.20 and rejected SPY candidate; it is not a downloaded historical dataset.
Missing-session coverage remains incomplete and aggregate metrics stay null.
Choose a new output directory when importing again; existing targets are never
overwritten. Manifest is written last as a completion marker. Interrupted
writes may leave partial directories, which the reader rejects; this is not a
crash-atomic database or recovery system. Evaluation outputs must remain outside
the source bundle, and source files/symlinked bundles cannot be overwritten.

Capture schema `orb-alpaca-capture-v1` requires `provider: alpaca`, explicit
`data_kind: synthetic|historical`, `retrieved_at`, `request`, and `pages`.
Each page has the request's `page_token` (null initially) and the response body.
Unknown fields, including headers/credential metadata, are rejected. The fixture
is a complete schema example. Request policy fixes SPY/QQQ, `1Min`, `raw`, USD,
ascending order, `asof: -`, explicit IEX/SIP feed and inclusive aware timestamp
bounds. Page limits are explicit. Requests must end at least a minute before
declared retrieval, and each bar must be completed and within bounds.

The token chain must terminate with null; missing/repeated/mismatched tokens,
duplicate bars, provider-order regressions, bad OHLCV and empty datasets fail.
Only after validating provider symbol/time order is it normalised to replay
time/symbol order. Decimal JSON prices never pass through binary floats. Raw
`n`/`vw` fields are retained in the capture but not used in the OHLCV strategy.
Extended-hours bars are retained; existing calendar replay decides eligibility.
No missing bars are interpolated or replaced with flat candles.

`evaluate-bundle` revalidates the raw capture, rederives the canonical CSV and
manifest, and compares them before evaluating. Its `orb-evaluation-bundle-v1`
identity binds the source archive as well as strategy/data/configuration.
Archive integrity does **not** authenticate Alpaca, verify user labels, prove
that a server supplied every bar, or establish original decision-time versions.
Both provider authenticity and point-in-time verification remain false. A
historical label alone never becomes verified provenance or profitability.
Retrospective bar revisions and feed differences still matter.

Official contracts checked 17 October 2026:
[historical bars](https://docs.alpaca.markets/us/reference/stockbars) and
[market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).
Alpaca returns symbol-first pages; token termination is separate from session
coverage. IEX is not the consolidated SIP feed. No feed entitlement is assumed.

## Bounded read-only historical acquisition

`fetch-alpaca` adds a GET-only client for the fixed HTTPS historical-bars endpoint.
It uses only `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` from the execution
environment, never command-line key arguments or credentials in captures.
Do not paste keys in chat, put them in Git, or upload an environment file.
There is no order endpoint, host override, automatic entitlement/feed fallback,
subscription purchase or live-money path.

Inspect a request without credentials, network or writes:

```bash
python -m orb fetch-alpaca \
  --start 2026-10-09T13:30:00Z --end 2026-10-09T13:47:00Z \
  --out-dir data/alpaca-pilot --plan
```

Once credentials and permitted service access are available, omit `--plan` to
fetch, validate and publish a new bundle for `evaluate-bundle`. The default feed
is explicitly IEX; SIP must be requested explicitly and requires applicable
entitlement. No connectivity, entitlement or free-service guarantee is inferred
from a successful offline plan. Requests must span at most seven days and end
at least 16 minutes before the acquisition clock. These are conservative local
policies, not claims about every provider plan.

Default bounds are 20 pages, 3 attempts/page, 5 MB/page, 25 MB total capture,
15-second network/read timeout and at most 10 seconds per retry wait. The plan
shows the maximum HTTP-attempt budget. `--max-pages` can adjust the page bound
within hard limits; other bounds are explicit `FetchLimits` API settings.
Timeouts are network/read bounds, not a strict whole-job wall-clock deadline.
The response reader checks its reading deadline between bounded chunks.

429 and selected transient HTTP/network failures retry only within budget.
`Retry-After` seconds/dates are respected; an excessive delay stops the request
instead of retrying early. Other transient failures use bounded backoff.
Authentication/entitlement failures stop immediately. Redirects are rejected
before authentication can be forwarded. Errors omit response bodies/credentials,
and credential echoes in a successful body are refused before archival.

No bundle is published until token termination and strict existing capture
validation succeed. Reaching a page/byte/retry cap never marks truncated data
complete. Existing destinations fail before network access. Response numeric
bytes remain exact inside the capture; headers and credentials are not stored.
The final exclusive bundle write retains the previously documented partial-write
limitations. Both authenticity/point-in-time flags remain false: a portable
archive does not cryptographically attest a completed TLS exchange or original
decision-time revisions.

Validation for this increment uses fake HTTP transports and a stubbed standard
HTTP opener, not a real provider run. No actual download or historical result
is claimed. Official historical-bars/authentication/entitlement references were
rechecked on 18 October 2026; links are above.

Real acquisition/evaluation is waiting for credentials or a supplied permitted
capture/bundle. Independent durable state/recovery work can continue meanwhile.
Broker paper execution still requires its own lifecycle and recovery evidence.

## Durable paper-entry intents and restart exercise

`orb.journal` is an independent persistence boundary, not a running broker
service or a checkpoint of the offline portfolio. It stores validated baseline
signals, whole-share quantities and a caller-supplied SHA-256 context identity
(configuration/evidence). It does not calculate or certify that identity's
content. A file is bound to one explicit `paper:<account>` scope and schema
version; a mismatched account, unknown version or foreign database fails closed.

`prepare` reserves one admitted entry intent per symbol/session. An exact retry
returns the existing row, while changed prices, quantity or context fail.
`claim` requires its expected revision, commits `dispatching` before returning,
and can succeed once. It refuses stale signals (>90 seconds after bar completion)
and submission at/after the entry cutoff. A future dispatcher must **also**
revalidate current risk, cash, data, kill state and the session before claiming;
the journal alone does not grant trading permission.

After stopping old dispatchers, explicit `recover` turns interrupted
`dispatching` claims into `unknown`. Unknown, accepted and partial intents must
be reconciled using the stable `EntryIntent.client_order_id` (`orb-<signal_id>`)
and broker evidence. A timeout or missing order lookup never implies rejection
and never makes an intent dispatchable again. Even a crash before the actual
send can therefore sacrifice an entry opportunity; avoiding blind duplicates
is the chosen safety tradeoff. Prepared intents remain evidence, not automatic
restart commands. Existing attempts are never released by changing sessions.

`observe` consumes adapter-normalised **cumulative** filled quantity/notional,
not incremental fills or average prices. Exact duplicate event IDs are harmless;
conflicting duplicates, backwards quantities/notional, changed broker IDs,
stale revisions and terminal amendments fail closed. A partial cancellation or
expiry retains filled shares and notional. `filled_shares` reports cumulative
entry-fill evidence, **not** current positions after exits. No cash, fees, exit
ledger, detector attempt history, daily-loss latch or risk reservations are
restored by this component. Rejected risk candidates still need durable
coordinator integration before the one-candidate rule survives a full restart.

Run the invented exercise into a **new** path (the parent must exist):

```bash
python -m orb recovery-demo --db results/recovery.sqlite
python -m orb journal-inspect --db results/recovery.sqlite \
  --account-scope paper:synthetic-demo
```

The demo persists two ten-share intents. SPY has two synthetic shares filled at
100.20 (200.40 cumulative notional), then the rest is cancelled; QQQ restarts as
unknown. Nothing is submitted or downloaded. Inspection does not run recovery
or modify lifecycle state, and missing files fail rather than being created.
The demo refuses existing paths. Operational SQLite files/companions are ignored
by Git; keep them outside source, and keep both project documents separate.

Local SQLite uses `BEGIN IMMEDIATE`, a five-second lock timeout, foreign keys,
`synchronous=FULL` and rollback journaling. Audit append and state/revision update
share a transaction. Competing connections may fail with a lock error; callers
must not send on any claim error. Use local filesystems, one connection per
worker, and stop old dispatchers before recovery. Schema migrations, distributed
leases, backups and protection against manual tampering are not implemented.
Abrupt subprocess-exit tests establish process-restart behaviour, not hardware
power-loss guarantees. Official design references:
[transactions](https://www.sqlite.org/lang_transaction.html) and
[atomic commit](https://www.sqlite.org/atomiccommit.html), checked 19 October 2026.

Next: connect durable candidate/risk decisions, account state and session loss
latches to a recovery coordinator with reconciliation gates. An actual Alpaca
paper adapter and real historical baseline evidence remain separate milestones.

## Durable offline account and risk coordination

`DurableCoordinator` now persists chronological session/minute observations,
accepted **and rejected** candidate decisions, and modeled full-fill account
commands. Reopening replays those saved commands through the existing
`AccountLedger`, `size_long` and `DailyLossGuard`, checking saved outcomes.
This restores modeled cash, fees, allocations, cash/exposure/stop-risk
reservations, consumed candidates and the fixed daily-loss reference/latch.
It does not query a broker or enable paper trading.

One outer SQLite transaction commits a command/decision together with any
intent preparation, research full-fill evidence or local withdrawal. Nested
journal operations use savepoints, not independent commits. A process stopping
between intent writing and command completion cannot leave a phantom
reservation. Each command has a unique caller-supplied request ID: exact
retries return the saved outcome; changed input under the same ID fails.
An exact candidate repeated under another request ID returns its historical
decision without a new reservation. That result is **not dispatch permission**.
A rejection stays consumed even if account capacity later returns.

Start each actual session at its exchange open, then explicitly advance the
minute clock/marks before account decisions. Held-symbol marks must be
available at that exact instant. Future marks fail; missing/stale held marks
latch unknown equity. A skipped held-inventory minute also latches uncertainty,
even if the next supplied price is favourable. Cash is known while flat.
Same-time fills update fees/equity before a later candidate. This is an explicit
offline command harness, not a replacement for the portfolio replay's event
scheduling or a stream/checkpoint adapter. Detector bars/ranges and pre-signal
PAUSED state are not checkpointed; every emitted candidate must reach the
coordinator durably before an external side effect.

Only the existing **full-fill/full-exit research ledger** is bridged. Synthetic
fills require a fresh local claim and are labelled `research:`; they are not
actual broker acknowledgements. `withdrawn` is a terminal local never-claimed
intent, not broker cancellation. Claimed/unknown/accepted/partial or otherwise
inconsistent external journal state gates new admissions and modeled lifecycle
updates without releasing resources. Recovery can latch an interrupted claim
unknown, but it cannot turn partial broker evidence into a reconstructed
portfolio. `broker_reconciled` and `paper_execution_enabled` remain false.

New coordinator files use schema 2. The existing journal reader supports
schemas 1 and 2; a nonempty schema-1 intent history is **not** adopted without
account evidence. The coordinator stores immutable configuration and executable
source/dependency identities. Changed configuration/model requires an explicit
migration, not reinterpretation of old events. Arithmetic uses a fixed
28-digit Decimal context. Replay is O(history) per command and is not optimised
for a production daemon. Event outcomes catch inconsistent replay, not all
manual edits/deletions; no authenticated audit, backup or migration system is
claimed. Use one authoritative local file and stop old dispatchers before
lifecycle recovery. Raw journal methods are lower-level building blocks, not a
way to bypass coordinator admission gates.

Create a new invented loss/restart example (the parent must exist):

```bash
python -m orb coordinator-demo --db results/coordinator.sqlite
python -m orb coordinator-inspect --db results/coordinator.sqlite \
  --account-scope paper:synthetic-coordinator \
  --daily-loss-fraction 0.001 --fee-per-share 0.01
```

The demo fills 24 synthetic SPY shares at 100.20, exits at 99 and pays 0.01/share
per side: modeled cash becomes 9970.72. The ten-unit daily-loss limit stays
latched across reopening, so QQQ is rejected. This is an arithmetic/recovery
exercise, not a market result. Inspection requires matching configuration,
refuses missing files and changes no lifecycle state. The demo refuses existing
paths. All existing strategy, replay and evaluation contracts remain unchanged.

SQLite savepoint semantics were checked against the official
[savepoint documentation](https://sqlite.org/lang_savepoint.html) on
20 October 2026. Tests include abrupt exit before/after the outer commit,
injected intent/command write failures, two competing connections, missing
equity gaps, retained aggregate risk and persisted rejected attempts.

## Partial-fill and exit reconciliation

`ReconciliationLedger` is the next offline recovery boundary. It consumes
adapter-normalised **cumulative** quantities, notionals and fees for partial or
terminal entries and exits. Deltas change modeled cash once; filled entry basis
is allocated proportionally across one or more exit orders. A cancelled partial
entry releases only its unfilled reservation. A partial exit can be cancelled
and replaced for the remaining whole shares. Pending and partially filled entry
orders retain conservative planned stop-risk capacity; a worse observed fill
can raise that commitment above plan. Exits release commitment only in
proportion to shares actually closed.

Every command has a stable request ID and is committed with its deterministic
outcome in a separate account-scoped SQLite file. Exact retries return the saved
historical result; conflicting reuse fails. Reopening replays all commands with
a fixed Decimal context and checks outcomes, configuration and source identity.
Evidence timestamps cannot move backwards. This is deliberately separate from
the schema-2 coordinator until migration and cross-file atomicity are designed;
it does not silently reinterpret an existing coordinator journal.

Recovery requires an adapter to supply one complete account snapshot containing
cash, all SPY/QQQ positions and all open ORB client-order IDs. Exact equality is
required—there is no cash tolerance or inferred terminal state. Missing or
unexpected orders, mismatched cash/positions, an incomplete/wrong-account
snapshot, or any `unknown` lifecycle keeps `broker_reconciled` false. A later
command invalidates an earlier match until another complete snapshot agrees.
Even an exact match leaves `paper_execution_enabled` false: reconciliation is
necessary evidence, not permission to trade.

Run the invented four-share exercise into a new path:

```bash
python -m orb reconciliation-demo --db results/reconciliation.sqlite
python -m orb reconciliation-inspect --db results/reconciliation.sqlite \
  --account-scope paper:synthetic-reconciliation --initial-cash 10000
```

The demo partially fills two SPY shares at total cost 200.42, cancels the other
two, then sells one share for net proceeds 100.99. After restart, a complete
invented snapshot matches cash 9900.57, one SPY share and the still-open exit.
Realised P&L is 0.78 and retained planned stop risk is 1.22. These are synthetic
accounting assertions, not strategy performance or broker evidence. No network,
credentials or order submission are involved.

Current limitations are intentional: the ledger is not yet atomically connected
to `DurableCoordinator`/`IntentJournal`; it trusts a future adapter's normalized
snapshot and has no authenticated broker receipt, distributed lease, backup,
migration, fractional-share, corporate-action, transfer, short, settlement or
multi-currency model. It tracks carrying-cost gross exposure rather than current
market value; fresh mark and daily-loss integration remains in the coordinator.
Each exit order is for currently open whole shares, so entry evidence must be
terminal before exits are planned. A future lifecycle adapter must map statuses
conservatively and never turn a missing lookup into rejection.

## Read-only Alpaca paper snapshots

`paper-snapshot` is a deliberately narrow bridge from Alpaca's paper environment
to `BrokerSnapshot`. It has no POST, PATCH, DELETE, host override or order method.
It reads credentials only from `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`, uses
the fixed `https://paper-api.alpaca.markets` host and rejects redirects. Inspect
the complete six-request budget without credentials or network:

```bash
python -m orb paper-snapshot --plan
```

An actual capture makes two consecutive passes over `/v2/account`,
`/v2/positions` and all open `/v2/orders`. Orders use the documented maximum
page size of 500 without a symbol filter; exactly 500 is declared incomplete
rather than assumed exhaustive. Each response is capped at 1 MB, all six at
6 MB and each request/read at 10 seconds. There are no retries, so a mixed-time
capture is not silently prolonged after a failure.

Only cash, whole long SPY/QQQ positions and every open client-order identity are
normalised. An unexpected symbol, short, fractional position, duplicate/invalid
identity, blocked/inactive account, pagination boundary or difference between
passes fails closed. Unsupported positions produce no lossy `BrokerSnapshot`.
Other incomplete captures carry `complete=false`, so the reconciliation ledger
can durably record the failed comparison without clearing its gate.

The account UUID is never emitted; its SHA-256-derived paper scope binds the
local ledger without exposing that provider identifier. Output contains hashes
of the six raw response byte strings, not bodies or credentials. Those hashes
support exact local capture comparison but do not authenticate Alpaca. Save the
redacted JSON externally if it is needed as an operational evidence artifact.

To compare an existing reconciliation database, with no submission side effect:

```bash
python -m orb paper-reconcile --db results/reconciliation.sqlite \
  --initial-cash 10000 --request-id recovery-check-1
```

The existing file must already be bound to the hashed paper account scope and
matching initial cash. Exact retries use the ledger's existing idempotency rules.
Even a complete matching capture still reports `paper_execution_enabled=false`
and `orders_submitted=false`.

Two equal passes are evidence of bounded quiescence, not an atomic provider
snapshot: an ABA change can theoretically occur between reads. No live provider
call was made in development; fake HTTP transports validate request policy,
normalisation and fail-closed behavior. Alpaca's official
[authentication](https://docs.alpaca.markets/us/v1.1/docs/authentication-1),
[account](https://docs.alpaca.markets/us/reference/getaccount-1),
[positions](https://docs.alpaca.markets/us/reference/getallopenpositions) and
[orders](https://docs.alpaca.markets/us/v1.1/reference/getallorders-1) contracts
were rechecked on 23 October 2026.

Next: integrate the adapter/reconciliation result with coordinator gating and
define conservative broker-status/correction recovery. Only after those controls
and explicit activation should a separately reviewable paper dispatch path be
considered. Real-data baseline evaluation remains a data-access dependency; no
live order or performance evidence exists.

## Durable coordinator snapshot gate

The offline coordinator now accepts the same `BrokerSnapshot` contract as the
partial-fill ledger and accepts `PaperSnapshotCapture` directly. Comparisons,
failed captures and fixed recovery hold codes are recorded in the coordinator's
own SQLite command transaction. They survive restart without rewriting cash,
reservations, inventory, risk or the session-loss latch. Try the credential-free,
network-free invented demonstration with a **new database path**:

```bash
python -m orb coordinator-gate-demo --db results/coordinator-gate.sqlite
python -m orb coordinator-inspect --db results/coordinator-gate.sqlite \
  --account-scope paper:synthetic-gate
```

The demo first retains a cash mismatch through restart, then obtains an exact
match, then proves that a terminal-correction hold still blocks a later matching
snapshot. It reports `orders_submitted=false` throughout. It is not a trading
performance example.

`observe_broker_snapshot(request_id, snapshot, at=minute)` checks exact account
scope, cash, whole-share positions and absence of unexpected open orders. A local
prepared intent is unsent, so it must not already appear as a broker open order.
Snapshots must be no more than 60 seconds old and never later than the explicit
completed-minute decision time. `observe_paper_capture` also bounds the start of
the entire capture window and retains incompleteness. This means the capture
must finish **before** the decision minute; a capture finishing afterwards cannot
be used to inform that earlier decision. This remains an explicit-clock offline
harness, without a live polling/scheduling loop.

`check_paper_account(request_id, at=minute)` uses the existing GET-only adapter
outside the SQLite writer transaction and then commits the result. It first
commits `capture_in_progress`, so a process exit during the read leaves the gate
blocked. Missing keys,
transport failures and adapter failures record `capture_unavailable`, retaining
no exception text. Exact request retries return the saved historical result
without another network call. Such a result is not current permission: inspect
`snapshot()` for the current gate. A network read made after the supplied minute
correctly remains blocked as future evidence. An event-loop integration should
capture first, advance the clock and marks to a later completed minute, then
call `observe_paper_capture` with the retained capture.

Once an observation or capture failure has enrolled the broker gate, every new
minute, session, admission or modeled account command makes the comparison stale.
The gate also fingerprints current journal rows, so a claim or external lifecycle
update through another connection invalidates a formerly matching comparison.
A stale or mismatched gate rejects new candidates and modeled account operations
without freeing resources. Rejections remain consumed for the symbol/session.
No observation is required for the existing standalone synthetic research path;
that path continues to report `broker_reconciled=false`.

`hold_recovery` records one of `order_status_unknown`, `terminal_correction`,
`fill_bust` or `external_activity`. These holds persist across sessions and cannot
be cleared by a cash/position snapshot or by retrying an old successful request.
There is intentionally no clear/force/repair method. The caller must retain a
hold before attempting any future correction workflow; this increment does not
map raw broker statuses, detect provider corrections or change terminal fills.
A separately reviewed migration/evidence-repair protocol is required.

This is a conservative comparison gate on the existing **full-fill research
model**, not atomic integration of the separate partial-fill ledger. Claimed,
partial or unknown external journal state stays blocked even if balances happen
to match. Synthetic fill history is explicitly labelled `synthetic_lifecycle`
and cannot be blessed as actual broker truth. A matching partial-ledger result
cannot authorise the coordinator. `paper_execution_enabled` is always false.

The executable model fingerprint now includes comparison and adapter code. Old
coordinator databases intentionally refuse reopening under changed rules;
preserve them and use their original revision to inspect them. There is no
in-place migration or historical reinterpretation. Do not delete state or edit
its fingerprint to bypass that refusal. The unchanged standalone reconciliation
ledger remains separate.

Next: a versioned, atomic integration of partial-fill accounting with admission,
intent and daily-loss state, including a reviewed correction-evidence protocol.
Real historical evaluation remains necessary before assessing strategy returns;
news/economic context remains deferred until that baseline exists.
