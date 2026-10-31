# Retrospective schedule and cost comparisons

The local platform can queue, execute, retain, inspect and export a bounded
comparison protocol. It uses the same Python evaluator and saved settings as
individual runs. No model parameter is tuned or selected automatically.

For an entirely invented, usable three-session source:

```sh
python -m orb.dashboard_api --examples --comparison-example
```

Select `synthetic-dashboard-comparison_three_sessions`, open Comparisons and
choose validation beginning `2026-10-13`. The synthetic source covers Friday
9 October, Monday 12 October and Tuesday 13 October. These are repeated invented
fixtures, not the user's SIP data or a performance study. Without this optional
flag, the existing three one-session examples remain unchanged. Actual source
bundles can be imported through the existing validated path.

## Freeze a deliberately small family

Select two or three saved schedule versions. Fixed ORB Reference must be first;
other profiles must differ only in eligible New York weekdays. Risk, capital,
R, caps and base costs must otherwise be identical. Reference and MWF are
available; an independently saved Tue/Thu custom variant can be selected. These
are transparent hypotheses, not copies or endorsements of named paid bots.
Non-schedule differences are rejected, not silently normalised or ignored.

The UI freezes two common cost scenarios: zero-cost diagnostic and an editable
illustrative market-side bps/per-share-per-side fee scenario. The shared plan
contract/CLI also supports a single scenario. Values use the existing exact
ResearchSettings validation; identical/unknown/invalid scenarios are rejected.
These inputs are assumptions, not provider quotes or calibrated execution costs.

Costs rerun sizing, fills, target calculation and exits. They are never merely
subtracted from a saved profit. Consequently net account outcomes need not be
monotone in a cost setting: a higher modeled fill also changes a fill-derived R
target, and whole-share quantities or exit paths can change. The synthetic
regression explicitly exercises this counterexample. Do not call a positive
stress difference evidence that higher real costs are beneficial.

## Chronology, partitions and missing evidence

Validation must begin on an actual exchange session strictly after source start
and within its declared bounds. Development ends the previous calendar day.
Both partitions retain every requested exchange session, including absent ones.
Holidays are not rolled forward. No minute bars are shuffled.

Each partition starts a separate flat account with the same starting capital.
This avoids using a later partition to fund earlier decisions, but is not a
continuous portfolio experiment. Unresolved holdings/data at a boundary remain
incomplete in that partition; validation's flat reset is an explicit experiment
assumption, not recovery of the earlier holding. Never concatenate the equity
curves or claim risk state continued across the split.

Imported source was already accessible for inspection. Reports therefore always
say **retrospective, unsealed validation**. Selecting a date does not create an
untouched final holdout, and already inspected 9 October data cannot become one.
No PBO/DSR, overannualised statistics, ranking or winner-selection logic is added.

Every profile/cost/period cell is retained. Wholly absent periods have
`missing_period_data`, no run ID and no fabricated zero return. Partial coverage,
unknown entry/holding evidence and missing marks retain the evaluator's existing
metric gates. A known no-fill can remain complete and flat, while expectancy
with no closed trades is undefined. A succeeded comparison job does not certify
market evidence or statistical significance.

## Metrics and units

JSON stores fractional returns, drawdown, exposure and risk utilisation; the UI
converts fractions to percentages. Markdown labels fractional return explicitly.
Undefined values say unavailable and include a reason. Each trial links to its
full candle/account/decision evidence.

- Counts distinguish candidates, closed trades and outcome classes.
- Net R uses closed net P&L divided by actual initial cost-aware stop risk from
  the filled account allocation, including the declared stop-side slippage and
  round-trip fee convention. It is not P&L divided by headline account capital.
- Full-period mean net R is withheld when evidence is incomplete. A separately
  labelled closed-subset mean is retained in the JSON; it is not a repaired
  full-period estimate. Even a complete small-sample mean is descriptive only.
- Initial risk utilisation is the observed entry's committed initial risk over
  its sizing risk budget, averaged over filled entries. It is not a gap-loss
  guarantee or an estimate for unobserved fills.
- Exposure uses the left/post-event minute sample within each observed session,
  with no overnight interpolation. It is a candle-grid approximation; actual
  intraminute exposure/drawdown can differ. Incomplete scopes withhold aggregate
  exposure, return and drawdown.
- Fees paid sum the account's observed entry and exit fees. Per-side assumptions
  and effective settings are frozen and displayed separately from the base
  preset, which is never rewritten.
- Cost sensitivity compares fully rerun outcomes only when both are eligible.
  Paired differences aggregate SPY/QQQ into New York sessions. If a period is
  incomplete, no favourable paired subset is silently salvaged.

The all-cash comparator is 0% with zero interest/costs. Confidence intervals,
session/block uncertainty estimates and passive/exposure-matched benchmarks are
explicitly unavailable and remain required for economic research. SPY/QQQ
same-session trades are not independent observations. No real-data edge is
established by the fixtures or by the user's unreproduced one-day SIP result.

## Persistence and compatibility

The existing local worker freezes plan snapshots, source and engine context.
All child runs, comparison summary and success status commit in one transaction.
A crash or capacity failure before commit publishes none of them; explicit
retry preserves the original failed/interrupted attempt. Prior worker locks,
engine/dependency gates, request idempotency and body/loopback guards remain.

The research DB upgrades additively from schema 1 to 2 inside a transaction,
adding only the comparisons table. Existing columns are checked before upgrade;
wrong/malformed schemas fail without migration. Old source/run/job rows and
preset identities remain unchanged. Preset and account/coordinator databases
are not migrated. As before, changed engine code invalidates pending old-engine
jobs but never silently recalculates saved historical results. Preserve closed
DB backups before upgrades; this is not an online backup or power-loss claim.

The worker remains explicitly requested and synchronous, with no acquisition,
scheduler or broker authority. A protocol has at most 3 profiles × 2 costs × 2
periods (12 cells) and at most 50,000 source-bar/profile/cost evaluations. Existing
100-run/1000-job/256-MiB archive bounds remain; comparison bytes count toward the
same budget. Larger work must be deliberately chunked rather than silently
truncated. The UI does not pretend to stream progress.

Child manifests use `orb-evaluation-comparison-trial-v1`, recording base preset,
common cost override and effective ResearchSettings. They can be inspected and
exported in the archive. Reproduction is through the frozen protocol and full
parent source; the ordinary uploaded-report importer does not accept this new
trial wrapper as an independent source bundle. Existing v4–v7/profile/bundle
imports are unchanged.

API: `/api/runs/{id}/queue-comparison`, `/api/comparisons`,
`/api/comparisons/{id}` and `/api/comparisons/{id}/report`. The queue request uses
exact preset IDs/versions; persisted protocols carry their copied snapshots.
CLI uses that same complete plan contract:

```sh
python -m orb.research_store --db results/dashboard-research.sqlite queue-comparison --id SOURCE_RUN_ID --plan plan.json
python -m orb.research_store --db results/dashboard-research.sqlite work-one
python -m orb.research_store --db results/dashboard-research.sqlite comparisons
python -m orb.research_store --db results/dashboard-research.sqlite export-comparison --id COMPARISON_ID --output comparison.md
```

The JSON export retains the full plan, all cells, paired sessions, sensitivity
and limitations. The UI never presents an overridden cost as if the saved
preset itself were changed. Historical/paper modes remain separate. News,
remaining timing/stop variants, uncertainty/benchmarks and atomic paper recovery
remain acceptance work; no provider, paid service or order was used here.
