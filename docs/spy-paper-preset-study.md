# SPY paper-preset historical study

This study aligns historical **instrument scope and saved settings** with the
SPY paper worker. It is a candle-based research proxy, not broker execution,
profitability evidence, recovery permission or a change to an existing run.

## Dashboard

Select a source in Simulation, then open **Details → SPY paper-preset study**.
Choose an exact saved preset, click **Evaluate SPY study**, and download its
JSON. Unsupported settings are explicitly rejected. Selecting a different
source/preset clears old results, including older in-flight responses.

The report is on demand, not a background job or paper Start command. Sources
and saved versions are durable; the study itself is downloaded/recomputed rather
than added to the job archive. Its JSON is a report, not an importable source
bundle. No permanent workspace panels or new trading controls were added.
The selected chart/run keeps its original settings and markers; the scoped
study's simulated trade paths are included in the JSON, not substituted silently.

## Scope and settings

The ordinary SPY/QQQ reference and its missing-symbol coverage remain unchanged.
Only this explicitly scoped study uses evaluation v8 with `symbols: ["SPY"]`.
All input bars remain in its source identity. QQQ is labelled excluded from
decisions/account/coverage and cannot consume SPY cash, risk or entry capacity.
Excluded duplicate bars and invalid chronology are still rejected rather than
hidden. No SPY data is an error. Missing SPY minutes/sessions and unresolved
outcomes withhold metrics; complete known no-entry sessions remain known zeros.
Missing QQQ does not invalidate this distinct SPY scope, but the original
two-symbol evaluation remains incomplete and unchanged.

The exact preset snapshot/fingerprint is validated, then checked by the real
worker's pure parameter validator. This creates no broker client, thread or
paper database and verifies neither entitlement nor recovery. Capital, risk,
exposure, aggregate risk, daily loss, weekdays, target R, entry/concurrency caps
and costs all pass through the existing research engine. Reference timing and
midpoint stop are required; research-only variants are rejected as in paper mode.
No new setting schema, preset value/version or automatic tuning was introduced.

The bounds are 20,000 source bars, 31 calendar dates and the import numeric
range. Hashes establish identity, not provider authenticity or point-in-time
availability. No provider acquisition or paid service is used.

## Prespecified cost and execution sensitivity

| Cell | Assumptions |
| --- | --- |
| Saved costs | Exact saved per-market-side slippage and per-share/side fee |
| Additional stress | Saved slippage +2 bps and saved fee +0.005 USD/share/side |
| One-minute delay | Saved costs; entry one minute after the normal entry boundary |
| Delay and spread | Same one-minute delay; assumed full spread of 4 bps, adding 2 bps per market side to saved slippage |

All four cells rerun account sizing, fills, fill-based targets, exits, costs and risk
gates. This is different from the paper cost report's post-hoc haircut on
observed fills. Outcomes/deltas need not be monotone when fills/targets change.
An override outside supported bounds remains a rejected cell, not a clipped or
omitted trial; each delta versus Saved costs requires both compared cells to be
eligible. The cost-stress delta separately requires the first two cells, while
`all_trials_eligible` covers all four. These are
declared assumptions, not actual broker fees, recommendations or selected winners.

The signal and frozen range/stop are unchanged. A delayed hypothetical market
entry is sized at its delayed open using the account state then available.
No order, cash/risk reservation or protective stop exists during the waiting
minute. Its high/low/close cannot decide whether to skip an earlier signal.
Every waiting minute and the entry minute must exist: a gap yields uncertain
`missing_entry_bar`, never a later convenient fill. A delayed boundary at or
after the entry cutoff is a known `entry_cutoff` skip. A delayed open at/below
the stop is a known no-entry. Missing coverage independently withholds metrics.

Full spread means ask-minus-bid, expressed in bps around an assumed reference
price; half is charged adversely per market side, **in addition** to saved
slippage. Avoid double-counting if that slippage already represents spread.
The same combined market-side cost reaches risk sizing and passive benchmarks;
the latter retain their stated open-to-close holding interval without ORB delay.
Limit targets still assume full fills at touches and favourable gaps. There is
no tick rounding, measured quote, partial fill, queue or pending-limit model.
One minute is a coarse stress assumption, not a measured worker latency.

JSON study/trial schemas are now v2; explicit engine scenarios use
`orb-fill-assumptions-v1` and evaluation v9. Dataset, preset, engine, scenarios
and effective costs are frozen in report identities. Existing baseline calls
keep their old schema/path semantics. Python callers may explicitly supply
`FillAssumptions(entry_delay_minutes=0..5, spread_bps=Decimal('0'..'100'))` to
`simulate`, `replay_portfolio` or `evaluate`; malformed assumptions are rejected.
These are research scenarios, not saved-preset fields or paper-worker settings.

## CLI

```bash
python -m orb.paper_alignment data/research/bars.csv \
  --start-session 2026-10-01 --end-session 2026-10-30 \
  --data-label "User-supplied historical archive" \
  --output results/spy-study.json --report results/spy-study.md
```

Paths/dates above are examples; no dataset is supplied. The default is the
unchanged Paper Balanced v1 snapshot. `--preset saved-preset.json` accepts an
exact saved record as returned by the preset API, not a bare settings object.
Use `--synthetic` for invented data. CSV is capped at 10 MiB and preset JSON at
8 KiB. Outputs cannot overwrite either input or each other. No credentials are
required or read for this operation.

## Remaining acceptance

Historical fills are still idealised boundary-open full fills with
conservative intrabar ambiguity. Quotes, freshness/measured-spread/marketable-limit tests,
liquidity, partial bracket activation, cancellation timing and actual fees are
not reproduced. Minute data does not establish executable or profitable returns.
Real-data provenance, untouched chronological tests, fill/timing sensitivity,
account-wide correction clearance, full fee attribution and prospective paper
observation remain required. No order, risk release, preset activation or
recovery authority is produced by a study.

## Verification — 16 November 2026

- Linux/Python 3.12.14 full suite: **1,389 passed**, five dependency warnings,
  123.96 seconds. The new study suite has 21 cases; study/evaluation focused run
  passed 42 cases.
- **64 grouped browser checks passed** with Chrome headless shell 157.0.8103.78:
  workspace 8, presets 15, research 4, comparison 5, paper 21, performance 6,
  alignment 5. Changed alignment checks were rerun after the final chart warning.
- TypeScript/esbuild, Python compilation, changed JS syntax, whitespace,
  study CLI help and dependency checks passed.
- Fresh editable installation and a separate fresh installed wheel passed.
  Python `-I` outside the checkout verified SPY scope, frozen settings/costs,
  full source identity, unchanged source, CLI/API and no worker/broker actions.
- Wheel SHA-256:
  `20ae414e61e2216c8516be1bbeffbf71ceb3768ef39ade1c793b2c3c48697243`.

All market/account inputs were synthetic/injected. No real Alpaca request/order,
Mac, CI or economic-performance verification is claimed. Five warnings are the
Starlette TestClient and calendar/Pandas deprecations. No preset values, worker
execution permissions, recovery holds or risk reservations were changed.
