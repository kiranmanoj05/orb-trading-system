# Versioned research presets

The local platform now has real selection, edit/save/duplicate/reset, historical
version inspection and application to a separate historical evaluation. This now covers **supported risk/cost inputs plus New York entry weekdays and
planned target R, session/concurrency caps, range duration, entry delay/cutoff
and midpoint/range-low stops**. Timing/stop variants are research-only. See trading-profile-variants.md for exact semantics and compatibility.

## Use in the dashboard

1. Open Applied settings and select ORB Reference. It is fixed and read only.
2. Duplicate saved preset. Edit the new variant's name and supported values.
3. Reset draft restores the selected saved version, without changing the DB.
4. Save new version validates every field on the server and appends a snapshot.
   An outdated expected version receives a conflict; reload before retrying.
5. Apply saved version to new run reevaluates the selected source with that exact
   version. Unsaved drafts cannot be applied. Loading/saving alone never rewrites
   an existing run, holding or outcome. Old applied versions remain frozen.

Duplicate uses the **saved** version, not unsaved edits. History can select an
old version for inspection or application; a stale version cannot overwrite a
newer head. The UI distinguishes saved selection, unsaved draft and applied run.
A transient preset-load error has a working reload path.

## Shared contract and limits

`ResearchSettings` v4 is authoritative for the new preset CLI, API/editor and saved
report imports. Settings JSON has exactly these fields (decimal/integer values are strings; weekdays is an integer array):

| Field | Unit / supported range |
| --- | --- |
| schema_version | `orb-research-settings-v4` (v1/v2/v3 retained unchanged) |
| initial_cash | USD, positive, at most 1e12 |
| risk_fraction | Equity fraction, greater than 0 and at most 1 |
| max_position_fraction | Allocation fraction, greater than 0 and at most 1 |
| max_gross_fraction | Gross allocation fraction, greater than 0 and at most 1 |
| max_total_risk_fraction | Initial stop-risk fraction, greater than 0 and at most 1 |
| daily_loss_fraction | Entry-block loss threshold fraction, greater than 0 and at most 1 |
| slippage_bps | Basis points per market side, 0–1000 |
| fee_per_share | USD/share/side, 0–1000 |
| target_r | Decimal string, planned price reward/risk, 0.25–10 |
| max_session_entries | Integer string `1` or `2`; filled-entry cap, pending holds a slot |
| max_concurrent_positions | Integer string `1` or `2`; pending/open/unresolved slots |
| weekdays | Nonempty ordered unique JSON integers 0–4 (Mon–Fri), NY session dates |
| range_minutes | Integer string `5`, `15` or `30`, anchored at 09:30 New York |
| entry_delay_minutes | Integer string `0`, `5`, `15` or `30` after range completion |
| entry_cutoff_local | `10:00`, `10:30`, `11:00`, `11:30` or `12:00`, New York |
| stop_policy | `range_midpoint` or `range_low`, fixed at signal time |

`0.005` means **0.5%**, not 0.005%. Exact decimal strings avoid silently using
binary floating-point values. JSON numbers, booleans, missing/unknown fields,
nonfinite/oversized values, more than 28 digits or exponents beyond 12 places
are rejected. Equivalent decimals and signed zero canonicalise consistently.
These are bounded research controls, not recommendations. Legacy evaluation CLI
flags remain available; the saved-preset path uses this explicit shared contract.

Risk/cost changes feed the existing sizing, portfolio and evaluation functions.
There is no duplicate TypeScript strategy. Position/cash caps can prevent using
the headline risk budget. Daily loss blocks new entries rather than guaranteeing
loss size or flattening inventory. Cost assumptions are scenarios, not measured
Alpaca execution costs.

The reference remains SPY/QQQ long-only, all eligible NY exchange weekdays,
[09:30,09:45) range, completed close strictly above frozen high before 11:00,
midpoint stop, planned 2R and one candidate/symbol/session. The independently
defined ORB MWF changes only weekdays to Monday/Wednesday/Friday. Custom v4
versions additionally support bounded timing and initial-stop variants. Old v1/v2/v3
records retain their fields/fingerprints; editing upgrades only the new saved version. Customisation is not evidence of economic improvement.

## Persistence and identity

The CLI defaults to `results/dashboard-presets.sqlite`; set `--preset-db` on the
dashboard to choose another local path. This is independent of the broker/risk
journal. Application/schema identifiers reject unrelated databases rather than
adopting/migrating them. File-backed writes use FULL synchronous DELETE rollback
journalling. Versions append under BEGIN IMMEDIATE and an exact head-version
check. Independent connections cannot both save over the same expected version.
Tests cover rollback injection and abrupt process exit before/after commit.

The fixed reference is version 1 of the code-defined baseline. Custom IDs are
random UUIDs, not performance labels. Each version includes settings schema,
canonical settings hash, name and actual creation timestamp. At most 100 custom
presets / 1000 versions per preset are supported in this local store. No delete,
automatic optimiser, broker permission, distributed lease, migration or power-
loss guarantee is implied. Data hashes detect inconsistency; this local SQLite
file is not an authenticated/tamper-proof audit log.

Applied runs contain a copied profile ID/version/name/settings/hash in the
manifest (`orb-evaluation-profile-v1`), plus their ordinary applied evaluator
values and data/engine identity. Source candles remain the same; explicit absent
sessions and bundle metadata are retained. No uploaded summary overrides the
engine. Repeated identical run identities retain their first immutable snapshot.
A paired saved profile report can be reimported with the matching original
source; it does not install a mutable preset in the database.

**Presets and historical run/job snapshots now persist** in their configured
local files. Queue saved version freezes the copied snapshot at queue time;
Research jobs can explicitly execute/retry it. Original inputs and old results
are retained across restarts. See [durable research](durable-research.md).
Account counters remain in the separate offline coordinator; archive persistence
never establishes broker reconciliation or paper execution permission.

## CLI

Activate the project's Python environment. The fixture settings are illustrative
cost assumptions, not a calibrated fee model. All repository price fixtures are
synthetic and their incomplete coverage remains visible.

```bash
python -m orb.presets template
python -m orb.presets --db results/dashboard-presets.sqlite list
python -m orb.presets --db results/dashboard-presets.sqlite create \
  --name 'Cost study' --settings fixtures/research_settings_example.json
python -m orb.presets --db results/dashboard-presets.sqlite run \
  --id orb-reference --version 1 --csv fixtures/orb_execution_sample.csv \
  --output results/reference-profile.json
```

Create/list print the custom ID. `save --id ID --expected-version N --name NAME
--settings FILE` appends a new validated version; it never updates a row in
place. `run --id ID --version N` requires a specific saved version and writes
its output exclusively, so existing inputs/results are not overwritten. The
bounded settings file limit is 8 KiB; run source limits match the local importer.
No credentials or optional web dependencies are required by this CLI.

## HTTP API

- GET `/api/presets`: server contract, current saved heads and persistence flag.
- GET `/api/presets/{id}/history`: immutable versions.
- POST `/api/presets`: `{name,settings}` creates, or
  `{id,expected_version,name,settings}` appends.
- POST `/api/presets/{id}/duplicate`: exact `{version,name}`.
- POST `/api/runs/{id}/apply-preset`: exact `{preset_id,version}`; returns a new
  or already retained evaluation identity. No browser-supplied candles/settings
  bypass the saved-version lookup on this endpoint.

Existing loopback Host/Origin, JSON, content-policy and size guards apply. Preset
mutations have an 8 KiB limit. No order, paper activation, news or credential
endpoint is added. Public hosting still requires separate design/authorisation.

Next: cost sensitivity and chronological comparisons/holdouts, followed by remaining
partial-fill account/recovery integration before enabling paper controls. Preserve the fixed reference and the distinction between software
verification, historical economic evidence and paper observation.


## v1 record compatibility

Existing v1 rows, hashes and saved snapshots retain their eight original decimal
fields exactly. They continue to mean all weekdays and 2R. The editor expands a
v1 record into an explicit v3 draft using those semantics; a user save appends a
v3 version and never rewrites the old row. Loading alone does not migrate data.
The built-in Reference stays at its original v1 identity, while ORB MWF is a
protected v2 preset. Their risk/cost/stop/entry defaults match. Applying explicit
v2 reference values still uses the unchanged baseline behaviour.


## Session-entry and concurrency limits

See [entry capacity and restart semantics](entry-capacity.md). v3 freezes both
limits into new runs; it never rewrites v1/v2 snapshots. Both controls are bounded
to 1–2 for the current two-symbol, one-candidate/symbol/session implementation.
Pending reservations hold slots, first modeled fills consume one session entry,
and exits release concurrency only. A cap never creates a trade or re-arms a
consumed candidate. Existing timing, costs, data/loss gates and tie ordering
remain authoritative. The durable offline coordinator serialises and reconstructs
these counters with cash/risk/intents; historical dashboard runs now use the optional separate research archive.
The separate bounded SPY paper worker supports the reference timing/stop window
only; it rejects timing/stop variants before acquiring credentials. External
partial-fill ledger integration remains unfinished.

## Timing and initial-stop semantics (v4)

See [timing and stop variants](timing-stop-variants.md). The first eligible
candidate candle starts at range end plus the delay; its close must complete
strictly before the NY cutoff, capped by the exchange flatten deadline.
Empty windows are invalid. All intervening minute bars remain required even
during a delay. A pre-window close does not consume the single candidate;
a later eligible close still strictly above the frozen high may qualify.
There is no additional cross-back-through-the-high requirement. Stop and target
management continues after cutoff. Range low is an initial fixed stop, not
a trailing stop. Wider stops can reduce sizing and alter outcomes.

The nested entry-policy v2 records every timing/stop choice. Current v6/v7
evaluation envelopes retain their structure and use this versioned nested
policy; source/model/settings identities distinguish results. The historical
schedule-only comparison rejects timing or stop differences rather than
confounding weekday attribution. This release adds capability, not a parameter
search or an economic recommendation.
