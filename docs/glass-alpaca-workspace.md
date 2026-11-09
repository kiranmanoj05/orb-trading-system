# Glass workspace and Alpaca integration

> Update: [Paper execution v1](paper-trading.md) supersedes the original
> observation-only boundary described below. Paper view now has its own current
> session chart, no backtest overlays, and hidden synthetic examples. Submission
> requires explicitly starting the paper worker in Settings.


The former local dashboard had no provider-facing route and did not discover the
user's existing CLI bundles. Starting with `--examples` therefore displayed test
fixtures rather than the downloaded SIP run. This change integrates the approved
minimal dark-glass design into the React app and makes source-driven results
accessible without exposing credentials in a browser.

## User flows

1. Run `python -m orb.dashboard_api` from the repository root after installing the
   dashboard extra and building `web`. Do not add `--examples` for normal use.
2. The server validates/recomputes complete bundles in immediate `data/` child
   directories and archives them. It never fills gaps or trusts uploaded metrics.
   The workspace selects the most recently imported complete non-example run when
   possible. Use the run selector for other dates and IEX/incomplete runs.
3. Data & Alpaca → Fetch & simulate: choose exchange-session dates and feed. The
   existing bounded acquisition adapter downloads original pages and the existing
   import/evaluation pipeline computes every candidate, trade, rejection and mark.
   The complete source package and immutable result enter the research archive.
4. Settings preserves validated selection/edit/save/duplicate/reset, history, apply
   and queue actions. Applying a saved version makes a new run; the source and old
   results stay immutable. Bounded research timing/stop settings are now supported; see timing-stop-variants.md.
   Paper execution still requires the reference timing/stop window.
5. Trades lists ALL engine candidates, not only winners or completed positions.
   Each row links to matching candles; details reveal reason, exact entry/stop/
   target/exit, quantity and fees. Candles are from the source; missing minutes
   stay gaps. Table access remains available. Expand/focus/full-session/pan/zoom
   support investigation without permanent interface clutter.
6. Data & Alpaca → Refresh paper account: observes broker equity/cash/positions and
   latest 500 open/closed orders. It never submits, cancels or modifies orders.
   Paper mode polls every 30 seconds only on a visible page. Failure leaves the
   last timestamp visible and reports an error. An average fill marker requires
   matching symbol, date and source candles. Unknown ORB attribution stays unknown.

## Safety and boundaries

Only `paper-api.alpaca.markets` and the historical adapter's fixed market-data
endpoint are used. No user-provided URL, server path, credentials or endpoint is
accepted through the HTTP API. Credentials are read from the backend environment.
The status endpoint reports configuration, not verified connectivity. Observation
is three bounded GETs and is not atomic; it does not clear the reconciliation gate.
Order history stops at 500 and reports possible truncation. Order averages are not
individual execution events. Non-SPY/QQQ orders remain listed; chart acquisition
is limited to the implemented SPY/QQQ universe.

The API retains loopback binding, Host/Origin checks and no CORS. Media serving
uses an explicit allowlist. CSP allows local blob media/images and inline styles
needed for canvas sizing and user-selected backgrounds; scripts remain self-only.
No recovery, accounting, strategy or submission semantics were changed.

The original uploaded default wallpaper remains 1600×1200 with SHA-256
`8cbde0d34be1796f7ebb94ee46f5b9a713253786eb52915a76645ca1b312f9b2`.
It is copied unchanged. User uploads remain raw Blobs; fit controls avoid cropping.
Glass panels intentionally blur only the backdrop inside the panels. Motion and
reduced-transparency preferences remain available; reduced motion is honoured.

## Verification limits

Provider tests inject bounded fake transports. Browser QA uses API-shaped fixtures
exported from actual engine evaluation; no real credentials/provider calls are
available in this runtime. Real-account access, actual Mac SIP reproduction and
Mac rendering require the user's local runtime and data. The hosted design study
remains a separate sample-data site; this change concerns the local GitHub app.

## Account fee records (9 November 2026)

The existing Trades drawer now contains an optional account fee panel and includes
fee versions/scan receipts in the saved-evidence export. It preserves Workspace /
Settings navigation, appearance/media handling and the opaque candle surface.
The panel is read-only, uses an explicit posting-window anchor and distinguishes
signed cash, partial reads and unavailable per-trade costs. See
[account fee evidence](paper-account-fees.md). Presets/settings are unchanged.
