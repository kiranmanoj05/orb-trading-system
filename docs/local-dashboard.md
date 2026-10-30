# Local historical dashboard

## Boundary and design

This is a real React/TypeScript client of the Python research engine, not an
execution service or a replacement for the separately hosted visual prototype.
Every trading result comes from the existing evaluator. The viewer cannot
submit orders, access credentials, fetch news, start paper trading or tune a
strategy. The fixed reference signal rules remain unchanged.

Original web CSS adapts the material/navigation principles from
[ECC's Liquid Glass design skill](https://github.com/affaan-m/ECC/blob/main/skills/liquid-glass-design/SKILL.md),
reviewed blob `495dd01a63bb523ad9b6d02f76fae2c4f2ab29e8` (MIT, Affaan Mustafa).
No Swift code or substantial skill text is copied. Glass is used for navigation,
controls and framing; chart/table evidence has sufficiently opaque surfaces.
Numbers have tabular alignment, statuses have text labels, keyboard focus is
visible, motion reduction is honoured and a persisted reduce-transparency
switch removes backdrop filters. Opaque fallbacks cover unsupported browsers.
React and React DOM licence text is emitted with the frontend build.

## Launch and verification

From the repository root, after activating the intended Python environment:

```bash
python -m pip install -e '.[dev,dashboard]'
npm --prefix web ci
npm --prefix web run build
python -m orb.dashboard_api --examples
```

Open `http://127.0.0.1:8765`. The CLI fixes the bind address to loopback and uses
one worker. Change the port with `--port`. Start without `--examples` for an
empty workspace. One command starts the built app; frontend assets require the
build step above and are not embedded in Python wheels. An installed wheel can
serve a separately built directory via `--web-dir /absolute/path/to/build`.

Run Python tests with both dev and dashboard extras installed. Without the
optional dependencies, API tests explicitly skip; core import tests still run.

```bash
python -m pytest -q
npm --prefix web run check
npm --prefix web exec playwright install chromium
npm --prefix web run test:browser
```

Browser verification starts and terminates its own local API on port 8877. Set
`ORB_PYTHON` to a particular interpreter or `BROWSER_EXECUTABLE_PATH` to an
existing Chromium binary. It tests complete/skip/missing cases, chart controls,
linked decision evidence, read-only settings, coverage, reduced transparency,
keyboard modal dismissal, mismatch rejection, actual import, escaping, export,
and mobile/tablet/desktop document reflow. It is not a complete accessibility,
contrast, performance or cross-browser certification.

## Import contract

`POST /api/import` accepts JSON with `kind`, UTF-8 source text, optional `label`
and optional `report` object. Supported kinds:

| Kind | Required source |
| --- | --- |
| `csv` | Canonical `symbol,start,open,high,low,close,volume` CSV |
| `capture` | Original `orb-alpaca-capture-v1` text in `source` |
| `bundle` | Original capture in `source`, plus `bars_csv` and `bundle_manifest` text |

Bundles are rederived from original capture bytes and compared with both files;
no archive extraction or server file path is accepted. CSV chronology and
minute/price invariants use the existing engine. A saved report must have an
internally consistent manifest fingerprint and an explicitly supported v4/v5
configuration. Bundle wrappers retain their base schema. Its canonical dataset
hash must match the source; claimed bundle identity must match the original
capture. Unknown settings are rejected rather than silently defaulted. Original
report summary, trades and P&L are ignored and regenerated. An edited manifest
with a matching edited hash is still merely a declaration, not authentic evidence.

The returned `import_context` distinguishes original report identity/schema from
the current evaluation ID and explains recomputation. v4 known skips can acquire
corrected v5 metrics without rewriting the original report. Exact applied
settings and their fingerprint remain immutable; the separate risk/cost editor
creates saved versions and new evaluations (see research-presets.md). Explicit saved date bounds
are retained, including wholly absent sessions. Without a saved report, coverage
uses observed New York dates only, matching the evaluator: omitted dates beyond
that scope are unknown. Capture request metadata remains visible but does not
silently change that scope. CSV synthetic/historical status is unknown regardless
of its label; capture data kind is an unverified declaration. All built-in examples
are conspicuously synthetic.

Local limits are 10 MiB of encoded request, 20,000 bars, 31 calendar days and 12
runs per server session. Additional numeric bounds prevent pathological Decimal
expansion; these are viewer bounds, not changes to the core strategy. Larger
research jobs require the future persisted job queue. No uploaded contents or
credentials are reflected in API validation errors.

`GET /api/runs` lists summaries; `GET /api/runs/{id}` returns complete engine
and dashboard evidence. Runs are immutable snapshots held in memory, not a
restart-safe store. Restart clears them. Exported dashboard JSON is review
output; it is not itself a source import (retain original CSV/capture/report).
No public hosting, authentication, durable jobs or multiworker behaviour is
claimed. The trusted local user can inspect all this session's runs.

Host and Origin checks reject browser DNS rebinding and cross-origin requests.
Writes require JSON, no CORS is enabled, assets have an allowlist, and responses
set no-store, nosniff, no-referrer and a self-only content policy. These guards
are tested, but are not an authentication scheme for public deployment. Run
through the provided CLI; arbitrary ASGI deployment is outside this boundary.

## Evidence and limitations

Candles use their start and explicit completion/availability time. Missing
minutes remain chart gaps. Frozen ranges appear only with all range bars.
Signal target and fill-based target stay distinct. Charts are retrospective;
viewing later candles does not make them earlier decision inputs. Null marked
equity breaks the equity path and never turns into zero. Closed-subset P&L is
visible even when aggregate return is unavailable, with its subset label.

Account panels distinguish end-of-run carrying commitments from marked equity.
Daily-loss states come from engine evidence. A historical snapshot cannot report
live freshness or broker reconciliation; the UI says so. Cost values are shown
with their units, including exact small per-share fees in applied settings.
Synthetic examples are arithmetic checks, not economic results. The user's SIP
pilot has not been reproduced here; permitted source bundles are still needed
for that independent research step.

## Next mandatory acceptance items

- Supported risk/cost selection, edit/save/duplicate/reset, draft/applied states
  and immutable per-run versions are implemented (research-presets.md).
  Complete the remaining strategy/schedule controls and durable caps before
  calling the broader preset acceptance criteria complete.
- NY weekday variants and target-R settings are now implemented with a fixed
  reference (trading-profile-variants.md). v3 now adds entry/concurrency controls and durable offline counters
  (entry-capacity.md); remaining broker partial-fill integration is required
  before exposing paper submission controls. Schedule gates entries, never position management.
- Durable research run/job storage, comparisons/cost stress and chronological
  holdouts. No automated chasing of yesterday's best parameters.
- Read-only paper snapshot integration, then recovery/reconciliation prerequisites
  before any separately authorised paper submission; news context remains later.

These follow the agreed platform roadmap (PR #19) and research audit. No new
user action is required to continue configuration and interface engineering.


## Preset increment (27 October 2026)

The CLI now configures a durable preset database by default. API tests without
a preset_db explicitly use temporary memory state; a configured local file
persists versions. The settings editor/API/CLI share ResearchSettings, including
exact decimal strings, schema and range validation, and rejection of unknown
controls. Applying a version creates a separate evaluator-backed profile run.
Preset metadata and applied values are frozen together; source/bundle identity
and explicit coverage scope remain visible. Duplicate identical run identities
retain their first stored snapshot. This is separate from durable job/account
state: imported and generated runs remain session-local.

Run the additional preset browser workflow with:

```bash
npm --prefix web run test:presets
```


## Weekday/target increment (28 October 2026)

The saved-preset flow now supports explicit v2 weekday and planned target-R
fields through CLI/API/UI and the same Python engine. ORB MWF changes only entry
days; custom schedules do not force trades or roll holidays into other days.
Inactive-day bars still feed coverage, valuation, account and daily-loss checks.
The target multiple is used at signal creation and recomputed from actual
modeled fill price for exits. Default reference traces stay identical; variant
configurations are explicit in v6 evaluation manifests and applied snapshots.
Prior v1 records/identities remain readable without database row migration.
