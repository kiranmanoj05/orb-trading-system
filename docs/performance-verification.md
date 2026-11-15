# Performance reporting verification — 15 November 2026

Verified on Linux, Python 3.12.14. All market and broker responses used for
verification were invented/injected. No real account, Alpaca request/order, Mac,
profitability or CI acceptance is claimed.

- Final full Python suite: **1,368 passed**, five dependency deprecation warnings,
  100.07 seconds. Twenty-one reporting cases cover cost arithmetic, exact
  notional/time agreement, unavailable totals, correction holds, source
  protection, local API access, deterministic block resampling, invalid saved
  preset fingerprints, and read transaction cleanup. Existing recovery,
  admission and management tests also passed.
- **59 grouped browser checks passed**: workspace 8, presets 15, research 4,
  comparisons 5, paper 21 and performance 6. New checks cover report downloads,
  small samples, incomplete data, run switching, evidence invalidation and late
  responses, responsive reflow and absence of report-triggered paper actions.
- TypeScript check and esbuild production build passed. Python compilation,
  changed JavaScript syntax, CLI/dashboard help, `git diff --check` and
  `pip check` passed.
- Built a wheel, installed it with dashboard dependencies into a separate fresh
  virtual environment, and ran Python with `-I` outside the checkout. Verified
  site-packages imports, historical reporting and uncertainty gates, owned-fill
  cost estimates from frozen settings, a consistent read snapshot, database
  reopening, local report API and no broker requests or orders.
- Final wheel SHA-256:
  `478fbccff548827135ff2225648d078f8c5b1ab55a41b272f216e67de00cb1e3`.

The earlier interactive session's system Chromium attempt was blocked before
creating a page by a denied process-singleton socket. This run downloaded the
official Chrome for Testing headless shell **157.0.8103.78** and completed all
listed suites. The failed setup attempt is not counted as a browser pass.
Browser fixtures are verification evidence, not trading performance.

The reports do not change strategy presets, order execution, risk release,
correction clearance or the workspace's visual design. Paper estimated costs
remain assumptions; full attributable fees/broker net P&L and general recovery
clearance are still unavailable. Historical SPY/QQQ research is not identical to
the SPY paper worker's quote/limit execution. Real data and prospective account
acceptance remain outstanding. See [release acceptance](performance-and-release.md).
