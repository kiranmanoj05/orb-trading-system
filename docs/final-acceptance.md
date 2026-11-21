# Final acceptance

Scope is frozen around the existing local research platform and bounded SPY
paper worker. Preserve the approved Workspace/Settings interface, media and
validated preset contract. Do not add strategies, tune parameters or open a PR
for every scheduled run. Consolidate essential corrections on the release
candidate; merge only after its applicable acceptance evidence is complete.

## Acceptance ledger

| Requirement | Implemented behaviour | Remaining acceptance |
| --- | --- | --- |
| Research and reporting | Chronological source recomputation; explicit missing outcomes; market marks; cost sensitivity; frozen trials and comparisons | Permitted multi-period data, declared development/validation/final-test periods and actual reports; no economic conclusion yet |
| Presets/settings | Reference, MWF and custom; select/edit/save/duplicate/reset; draft versus applied; immutable versions; shared engine validation | Keep existing automated coverage; unsupported paper timing/caps must continue to reject explicitly |
| Dashboard | Source candles, decisions, trades, account and risk views; separate simulation/paper modes; local bundles and exports | Existing full-stack fixture suites verify behaviour; actual Mac/provider checks remain separate |
| Execution/risk | SPY paper only; saved intent/reservation before dispatch; bounded quotes/limits/brackets; durable entry/loss gates | Actual account entitlement and supervised signal/order/fill/protection/exit lifecycle |
| Recovery | No blind retry; agreeing inventory reads; retained unknown outcomes; terminal accounting/retirement and explicit account-wide review of supported terminal corrections | Unsupported identity/quantity/fee/cash/history corrections stay blocked; actual provider restart/disconnect acceptance is absent |
| Fees/net profit | Per-side simulated costs; conditional paper estimates; separate fee and cash evidence | Complete attributable broker fees and cash reconciliation are unfinished; missing costs must remain unknown |

The final three outstanding categories are real release limitations, not optional
polish. A fixture pass does not close them. A complete account lifecycle may
validly contain no entry; a no-trade observation cannot establish fill, protection
or exit acceptance. A strategy can fail economic evaluation while its software
works correctly. There is no guarantee that parameter changes make it profitable.

News/calendar context remains pending its documented baseline-evaluation and
entitlement prerequisites; it has not been delivered or silently removed from
the backlog. New providers, AI, new markets, public hosting and visual redesign
are outside the finalisation work. No paid service or live-money order is
authorised.

## One local engineering check

With `.[dev,dashboard]` installed and an existing Chromium/headless-shell binary:

```bash
python scripts/release_check.py \
  --browser /absolute/path/to/chrome-headless-shell \
  --output /absolute/path/outside-the-checkout/orb-acceptance
```

The output directory must be new and outside the checkout. The command runs the
full Python suite, dependency check, locked frontend install, TypeScript/build,
compilation, CLI help, whitespace and all eleven browser variants. It
uses injected broker/market data, removes Alpaca credential variables from child
environments and never starts the ordinary paper worker. It does not download a
browser. It retains each command's real exit code, elapsed time, log and SHA-256.
Failure/timeout stops later checks and records them as not run. An interrupted
run retains `complete=false`; source changes during verification prevent a pass.
HEAD/tree and a digest including uncommitted source are recorded so dirty work
cannot be represented as a tested published commit.

This command is a developer check, not a requirement for the user to repeat on
their Mac. It does not verify installed-wheel isolation, CI, real data, provider
connectivity or profitability. Retain separate evidence for those claims.

## Run in VS Code

Open the repository folder and use **Terminal → New Terminal**. From a clean
checkout of the reviewed release (Python 3.11+ and Node 22+):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,dashboard]'
npm --prefix web ci
npm --prefix web run build
python -m orb.dashboard_api
```

Select `.venv/bin/python` in VS Code's Python interpreter selector. Open
**http://127.0.0.1:8765**. No keys are required to import saved research bundles;
existing complete `data/*` bundles are discovered locally. Credentials, when
needed for account observation, must be configured securely in the backend
terminal environment before starting the server, never in chat or frontend code.

Launching the server does not start trading. Before explicitly starting paper
submission, the user must choose the intended saved capital/risk preset and an
entitled feed. Keep the server running and machine awake. Start before the range
ends at 09:45 New York; the worker will not buy an earlier missed signal. Follow
[the supervised-session checklist](performance-and-release.md#first-supervised-account-session).
Do not delete journals or force a retry to clear a blocked state.

## Remaining sequence

1. Keep this as one release candidate; close essential software accounting and
   account-recovery gaps together, with explicit refusal for unsupported cases.
2. Run the engineering gate against the final source and separately verify an
   isolated wheel. Recheck concurrent changes and remote checks before publishing.
3. Record supervised provider lifecycle/recovery evidence and a reproducible
   historical evaluation when permitted runtime/data are available. Do not
   replace missing evidence with terminal snippets or synthetic profits.
4. Close the acceptance ledger only with evidence, then stop recurring development.

User input is needed only for the account/data-dependent steps: secure local
credentials, the chosen paper capital/risk/feed, and local permitted archives.
Never request keys in chat or require uploads just to keep local UI work moving.

The supported terminal-correction permission policy is now implemented on this
candidate; see [conditions, re-blocking and limits](terminal-correction-clearance.md).
It preserves daily-loss/session controls and requires explicit Start afterwards.
It does not close the attributable-fee or actual provider acceptance requirements.
