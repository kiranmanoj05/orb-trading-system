# Research timing and initial stops

In Settings → Strategy & risk, duplicate a fixed preset, choose supported values,
save a version, then apply it to a new historical run or queue it. Loading/saving
alone never changes the chart, previous runs, pending jobs or a paper position.
The fixed Reference remains [09:30,09:45), no delay, completed close strictly
above frozen high before 11:00 NY, midpoint stop and planned 2R.

| Setting | Supported values | Meaning |
| --- | --- | --- |
| Opening range | 5, 15, 30 minutes | Exact consecutive completed minutes from 09:30 NY |
| Entry delay | 0, 5, 15, 30 minutes | Added to range end; gates candidate candle starts |
| Entry cutoff | 10:00, 10:30, 11:00, 11:30, 12:00 NY | Completed close strictly before cutoff, capped by flatten |
| Initial stop | Range midpoint, range low | Fixed from the completed range; no trailing/volatility stop |

A range of 5 minutes with a 15-minute delay permits candidate candles starting
at 09:50, whose closes first become available at 09:51. Earlier bars must still
be present and consecutive. A close above the high during the delay is not a
candidate and does not consume the attempt; an eligible later close above the
high can qualify without another crossing event. A 30-minute range plus a
30-minute delay with a 10:00 cutoff is invalid, rather than an empty strategy.

Missing/duplicate range minutes, missing intervening minutes and future/incomplete
bars retain the existing safety gates. Holidays do not roll forward. NY times use
the exchange session across DST and early closes. One candidate per symbol/session
remains fixed. Entry windows/weekday gates do not interrupt existing position
management. Conservative stop-first handling remains when a candle touches both
stop and target; minute bars cannot establish the actual intrabar order.

Initial stop choice feeds the same cost-aware sizing and fill-based target rules.
A range-low stop is usually wider than midpoint and may change quantity, capital
utilisation, target and exit path. Neither setting is claimed safer or profitable.
No new historical market experiment or automatic best-preset selection was run.
Cost/latency realism, untouched holdouts and uncertainty remain separate research
requirements. The schedule-only comparison deliberately rejects timing/stop
changes so its weekday attribution stays isolated.

## Shared contract and reproduction

`orb-research-settings-v4` adds four required fields; decimal/integer settings
remain strings. The nested `orb-entry-policy-v2` is explicit. Old v1/v2/v3
preset records and v1 entry policies retain their original serialization and
fingerprints. Saving an edited copy creates a new immutable version. Reports,
source imports and restored jobs validate/reproduce the exact policy; unsupported
fields and empty windows are rejected, never silently ignored. Existing evaluation
envelope versions are retained because their config structure still contains a
separately versioned entry policy. Source/settings/model identities record the change.

```bash
python -m orb.presets --db results/presets.sqlite create \
  --name '5m range-low experiment' \
  --settings fixtures/research_settings_timing_example.json
python -m orb.presets --db results/presets.sqlite run \
  --id PRESET_ID --version 1 --csv fixtures/orb_execution_sample.csv \
  --output results/timing-experiment.json
```

Use the actual ID returned by create. CLI/API/UI share `ResearchSettings` and the
Python detector/simulator. The chart uses the selected range; Details shows effective
range/window/stop separately from fixed reference rules. A selected run cannot
borrow the prior run's chart/settings while loading.

## Execution boundary

This is research capability. The bounded SPY paper worker accepts only reference
timing/stop settings (including equivalent v4 records); changed choices are refused
before credentials/client creation. The offline durable journal also remains baseline
only and rejects variant signal identities. Neither interface silently trades a
variant with reference rules. Broker execution/P&L ledger and atomic correction
integration remain unfinished. Tests use invented candles and injected responses;
no Mac, real-data profitability or Alpaca execution is established here.
