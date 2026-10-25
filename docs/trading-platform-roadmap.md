# ORB trading platform roadmap

Agreed product direction: 25 October 2026. This is planned scope, not an
implemented dashboard or an enabled execution service.

## Product goal

Make strategy decisions, risk limits, data quality and outcomes visible in one
local web platform. Build a research and paper-trading control centre around the
existing Python engine. Prioritise explainability and operational visibility.

The existing fixed SPY/QQQ long-only ORB remains the reproducible reference.
Configurable variants are new versioned experiments. Do not change baseline
results retroactively or present unsupported strategies as available.

## Dashboard visual direction

Use the user-selected ECC liquid-glass-design skill as a visual reference:
https://github.com/affaan-m/ECC/blob/main/skills/liquid-glass-design/SKILL.md
Reviewed blob: 495dd01a63bb523ad9b6d02f76fae2c4f2ab29e8, 25 October 2026.
This skill is written for iOS 26 SwiftUI/UIKit/WidgetKit, not React. Adapt its
principles to the web; do not treat Swift APIs as browser dependencies or claim
CSS reproduces the native Apple material exactly.

- Default proposal: dark graphite/navy foundation, restrained cool accent,
  rounded translucent navigation, toolbar and profile-selector surfaces.
- Keep candles, price tables, risk values and decision logs on solid or
  sufficiently opaque panels. Readability takes priority over visual effects.
- Glass is a navigation/control layer, not a treatment for every card. Avoid
  nested blur and excessive backdrop filters; restrict and measure blur areas.
- Use clear typography and aligned tabular numbers; show units, timestamps,
  positive/negative signs and status text. Never encode profit/loss, execution
  mode, stale data or risk limits with colour alone.
- Hover/press feedback only on interactive controls. No morphing position for
  active trading actions, blinking P&L or motion that disrupts chart inspection.
- Honour reduced-motion preferences and provide an explicit reduce-transparency
  setting. Supply opaque fallbacks when backdrop filtering is unsupported.
- Check keyboard focus, zoom/reflow, responsive layouts and text contrast
  (minimum 4.5:1 for normal text, 3:1 for large text) against actual backgrounds.
- Paper/historical mode and execution capability remain visible at all times.
  Destructive paper controls use distinct labels, not decorative glass icons.
- Build the first slice in React/TypeScript with reusable design tokens and
  components; chart rendering and backend/data contracts remain independent.

ECC is MIT licensed (Copyright 2026 Affaan Mustafa). This brief references and
adapts ideas rather than copying its Swift code. If later implementation vendors
code or substantial text, retain its required copyright and MIT notice.

## Standing product requirement: presets and settings

Confirmed by the user on 25 October 2026: presets and configurable strategy/risk
parameters are mandatory acceptance criteria for the platform. Keep them in
every development handoff and progress report. The platform is not complete
with a read-only dashboard alone.

Provide named preset selection, editable supported parameters, validation,
save/duplicate/reset, draft versus applied state and immutable per-run versions.
Expose schedule, opening range/entry window, supported stop and target policies,
target R, per-trade risk, entry/concurrency caps, account exposure/aggregate risk,
daily-loss limits and execution-cost assumptions as their engine support lands.
Use one configuration contract across CLI/API/UI; never build settings that
silently do nothing. Preserve the fixed reference preset and make unsupported
options explicit. Changing a setting is a research variant, not evidence of
better returns. Validate economically through the recorded evaluation protocol.

Every development update must state what changed, checks actually completed,
progress on preset/settings acceptance, next increment and essential user
action (or none). Continue independent implementation autonomously. When a
credential, inaccessible artifact, decision or budget is essential, notify the
user promptly with the smallest concrete action; pause only dependent work and
avoid repeated blocker notifications. Existing authorisation remains in effect.

## First usable dashboard

- Overview: distinguish historical replay, paper observation and later paper
  execution. Show run dates, account identity, starting capital, data feed,
  observed time, market state, completeness and execution capability.
- Keep the $10,000 historical simulation separate from the observed $100,000
  paper balance. Historical replay is not broker activity.
- Chart: candles, frozen opening range, signal time, modeled/actual fills,
  stop, target and flatten deadline. Every marker links to its evidence.
- Decision timeline: waiting, eligible, signalled, rejected, unfilled,
  unresolved, partially filled and closed. Explain skipped entries too.
- Portfolio and health: cash, reservations, holdings, exposure, planned stop
  risk, daily-loss state, price freshness, reconciliation gate and errors.
- Results: net/gross P&L, costs, return, drawdown, trade count, win fraction,
  average R, expectancy and profit factor when meaningful. Label unavailable
  statistics, small samples and incomplete evaluations explicitly.
- Import existing evaluation JSON and its source bundle through a local service.
  Preserve fingerprints and report limitations. An imported capture is a
  snapshot, not a live stream. Show loading, empty, stale and error states.
- First acceptance: inspect a complete SIP run and an incomplete IEX run,
  understand why their metric eligibility differs, inspect a trade, and inspect
  paper account state without terminal JSON. Existing report evidence is a
  starting point; acquire the actual files before claiming reproduction.

## Configuration backed by the engine

- Versioned profiles: starting capital, risk per trade, position/gross exposure
  caps, aggregate stop risk, daily-loss limit, fees and slippage assumptions.
- Add engine support for maximum entries per session across symbols, maximum
  concurrent positions and a precise attempt/re-entry policy. Define how pending
  reservations and partial fills consume limits; persist counters across restarts.
  A maximum trade count is a cap, never a daily target to force entries.
- Add validated variant settings for target R multiple, opening-range duration,
  eligible entry window and supported stop policy. Explain 2R as planned reward
  twice planned stop risk, not twice account capital or a guaranteed payoff.
- Preview dollar risk, share sizing and relevant caps before starting a run.
  Explain when a position cap prevents using the full per-trade risk budget.
- Preserve an immutable applied configuration with every run/decision/trade;
  separate draft/saved/applied settings. Changes take effect at an explicit run
  or session boundary, never silently alter an existing position or past report.
- Unsupported styles remain unavailable. Begin with ORB variants, with explicit
  parameter values rather than vague conservative/aggressive marketing labels.
  Swing, mean-reversion and short-selling require separate strategy design and
  evaluation, not a cosmetic selector.

## Trade review and replay

Show entry/exit reasons from structured engine events; order lifecycle; planned
versus filled prices; costs; realised R; holding time; and adverse/favourable
excursions with candle-resolution limitations. Rejected and unresolved outcomes
must remain visible. Add replay controls and saved-run comparison using the same
data interval and costs. Use time-separated holdouts before parameter selection;
never choose the best profile solely from the final evaluation period.

## News and economic events

Start by displaying context alongside trades, not by changing trading decisions.
Check Alpaca news entitlement first; official docs describe Benzinga articles.
For broad ETFs, include macro headlines and major constituent context rather
than relying only on SPY/QQQ ticker tags.

Keep publication, update, retrieval/first-observed times, source URLs and symbol
associations. Historical availability must be evidenced: retrieving an old
article today does not establish its original text was available then. Provider
outages and stale news/calendar data must be visible. Distinguish context from
an actual causal entry reason. Compare baseline and event-filter variants later.

Evaluate US CPI, employment and FOMC schedule coverage and historical licensing
before selecting an economic-calendar provider. No subscription purchase is
authorised by this roadmap.

## Optional AI analyst

An LLM is unnecessary for execution, charts, risk or rule explanations. Add it
later only for source-linked news summaries and post-trade narrative based on
verified events. Separate interpretation from recorded facts. It must never
invent causes, place orders, change limits or claim to predict profitability.
Historical reports cannot use later article revisions or future context.
Any paid model/provider use needs budget authorisation.

## Implementation sequence and boundaries

1. Dashboard data contracts and local read-only UI: existing reports, chart,
   decision evidence, health and paper snapshots. Suggested stack: React with
   TypeScript, a Python FastAPI service, existing SQLite persistence.
2. Versioned configuration and missing engine controls; run launch/status,
   comparison and replay through the same validated engine as the CLI.
3. News/calendar context and trade review. Integrate economic filters only
   after their historical information timing can be evaluated correctly.
4. Paper execution controls after recovery, reconciliation and order lifecycle
   support are verified. Distinguish pause-new-entries, cancel-pending and
   flatten-positions; do not label a pause as a full emergency liquidation.

Dashboard implementation now proceeds alongside historical validation and the
remaining recovery work. First deliver a useful read-only interface; do not wait
for the whole broker executor. Never make decorative controls that claim to
modify unsupported engine behaviour. No live-money trading is authorised.

Keep provider credentials on the server, never in browser bundles, browser
storage, Git or reports. Initially bind the service locally. Public hosting
requires authentication and a separate deployment decision. A browser refresh
must not restart a strategy or submit duplicate orders. Background work belongs
to the backend, and the UI displays persisted job/order state.

## Named profiles and daily delivery plan

### Competitor research, 25 October 2026

The public Bitcoin Playboy Linktree links PlayBit Trading Bots and Trading With
Robots. The PlayBit listing describes automated ILM and ORB models. Trading With
Robots describes a dashboard, pause/stop controls and a TradingView/execution
partner stack oriented towards prop accounts. These are vendor descriptions,
not independently verified performance evidence. Public material inspected did
not establish the exact Hunter or Safe Flow parameter sets, weekday schedule,
or representative net returns. Do not reconstruct proprietary rules from names
or transfer futures/prop-account performance claims to SPY/QQQ share trading.

Sources:
- https://linktr.ee/pb.ema
- https://whop.com/playbit/playbit-trading-bots/
- https://start.tradingwithrobots.com/

Adopt named, transparent, versioned profiles and visible activation schedules.
Defer ILM, ICT/liquidity concepts and market-regime routing until they have
precise causal definitions and separate evaluation. Never market a profile as
safe or profitable because of its name, weekday schedule or selected payouts.

### First research profiles

| Profile | Entry eligibility | Purpose |
| --- | --- | --- |
| ORB Reference | Every eligible exchange weekday | Preserve current fixed baseline |
| ORB MWF | Monday, Wednesday, Friday | Isolate weekday eligibility as one experiment |
| ORB Custom | Explicit supported settings | Save user-selected research variants |

For the first comparison, Reference and MWF share identical instruments, entry
logic, costs, target R and risk rules. Use America/New_York session dates, apply
exchange holidays/early closes, and never move a holiday opportunity to another
day. Everyday means eligible to seek a setup, not a promise of a trade. Reject
unsupported weekend selections. A schedule gates new entries only; management
and reconciliation of existing holdings continue on every required session.

Compare profiles one at a time on the same chronological dataset. When multiple
profiles eventually operate concurrently, they must share one durable account
risk ledger and reconcile duplicate same-symbol signals; labels cannot multiply
available cash/risk. Do not enable concurrent-profile execution in the first UI.

Weekday selection can reduce exposure while reducing opportunity; it does not
establish an edge. Report absolute account outcomes alongside per-trade net
expectancy in R, trade count, exposure time, drawdown and uncertainty. Keep a
chronological holdout closed during selection; use walk-forward comparisons and
disclose all tried variants. Do not auto-select yesterday's winner for today.

### Delivery sequence

Session numbers are planning ranges, not fixed completion promises. Inspect
current repository state and adjust around findings. Finish coherent changes
before opening another branch; a daily run does not require a new branch.

| Sessions | Deliverable and acceptance | User dependency |
| --- | --- | --- |
| 1-2 | Investigate editable-import failure; establish dashboard contracts and reproducible complete/incomplete fixtures. Record verified runtime support. | Actual permitted capture/report bundles needed to reproduce the user's runs; synthetic fixtures can proceed |
| 3-5 | Local API and dashboard: run import, overview, health, candle chart, range/fill/exit overlays and decision details. One documented launch command. | Optional interface feedback; no routine user verification prerequisite |
| 6-9 | Versioned engine-backed profiles; NY weekday eligibility; target R settings; maximum session entries and concurrent positions; durable counters and shared risk. Disabled settings must explain missing support. | No user input needed for research-only defaults |
| 10-12 | Queue historical jobs; compare Reference vs MWF with costs and immutable settings; show incomplete data and held-out evaluation separately. | Historical access in executor or permitted saved bundles for real-data runs |
| 13-15 | News feed and economic events with source/timing evidence, outage states and trade-context links. Context first; evaluate filters separately. | Entitlement check; user approves any paid plan before purchase |
| 16+ | Complete reconciliation/partial-fill/correction dependencies; integrate paper controls and forward observation with restart and duplicate-submission checks. | Secure credentials in the actual runtime and a user-selected paper capital/risk configuration before enabling submission |

These ranges are an initial backlog of roughly 15-25 substantive development
sessions plus paper observation time, not a promise of a profitable strategy or
a completed live-money product. If the dashboard's first vertical slice can
ship earlier, ship it without waiting for every control.

### Daily coding instructions

1. Read live source/PRs, this roadmap, recent user updates and separate private
   report/guide. Inspect actual branch heads; preserve other changes.
2. Choose the earliest unblocked acceptance item. Deliver an implementation
   increment with meaningful tests/checks and a working vertical slice where
   feasible; avoid recurring documentation-only sessions when coding is possible.
3. Keep engine rules authoritative for CLI, API and UI. Validate settings on the
   server, persist applied versions, and expose exact decision reasons.
4. Exercise relevant behaviour, including incomplete/stale input, restarts,
   concurrent admissions and unsupported settings. Report only checks actually
   performed. Use synthetic fixtures explicitly where real data is inaccessible.
5. Publish coherent lowercase commits/PRs within existing authorisation, inspect
   changes/required checks before merging, and update the private report/guide.
6. Notify the user after every session: delivered behaviour, evidence, PR/commit,
   next milestone and precise required input. If blocked, finish independent
   work, request the smallest missing input once and track the dependency.
7. Advance the backlog from evidence. Do not optimise on the final holdout,
   fabricate fills/performance, purchase services or activate live-money trading.

### Input checkpoints

- Optional now: product screenshot or old guide that names the remembered modes.
  This improves fidelity but never blocks independently defined ORB profiles.
- Needed for reproducing real results: upload the permitted IEX and SIP bundle
  directories and evaluation JSON/Markdown, excluding credentials/environment
  files. The provider downloads on a Mac are not present in this runtime merely
  because terminal output was shared.
- Before paid integrations: choose a budget only after free/existing access and
  exact required coverage have been checked. No AI key is needed for phase one.
- Before paper submission: confirm intended paper capital and risk limits,
  configure runtime secrets securely and review the concrete operational setup.
- Before 24/7 hosting: choose runtime/deployment and approve any recurring cost;
  local platform development continues meanwhile.

The first accepted platform version requires real run viewing, chart/evidence
navigation, validated profile-based backtests, cost-aware comparisons and clear
operational state. Later completion requires verified paper order lifecycle and
observed recovery behaviour. Broader markets/new strategy families are separate
scope decisions after these milestones.

## Other reference ideas

- QuantConnect results: linked equity, holdings, orders, trades and logs.
  https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/results
- Freqtrade: web backtesting, exportable results and frozen run configuration.
  https://docs.freqtrade.io/en/latest/backtesting/
- Alpaca news documentation and endpoint:
  https://docs.alpaca.markets/us/docs/historical-news-data
  https://docs.alpaca.markets/us/reference/news-3

These references inform interaction patterns, not claims that ORB already has
their capabilities or that their strategies apply to this market.
