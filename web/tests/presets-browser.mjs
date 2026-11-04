import { researchFixtures, simulationView } from "./research-view.mjs";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url)),
  temp = await mkdtemp(join(tmpdir(), "orb-presets-"));
const base = "http://127.0.0.1:8878";
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  [
    "-m",
    "orb.dashboard_api",
    "--examples",
    "--port",
    "8878",
    "--web-dir",
    root + "/web/build",
    "--paper-db",
    temp + "/paper.sqlite",
    "--preset-db",
    temp + "/presets.sqlite",
    "--research-db",
    temp + "/research.sqlite",
  ],
  { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
);
let logs = "",
  browser;
server.stderr.on("data", (b) => (logs += b));
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw Error(logs);
    try {
      const r = await fetch(base + "/api/runs");
      if (r.ok) {
        ready = true;
        break;
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  assert(ready, logs);
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_EXECUTABLE_PATH
      ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH }
      : {}),
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--single-process",
      "--no-zygote",
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
    ],
  });
  let activeRunId;
  const page = await browser.newPage({
      viewport: { width: 1500, height: 1000 },
      reducedMotion: "reduce",
    }),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await researchFixtures(page);
  await page.goto(base);
  await simulationView(page);
  await page.getByLabel("Selected run", { exact: true }).waitFor();
  const list = await (await fetch(base + "/api/runs")).json(),
    sourceId = list[0].id;
  const old = await (await fetch(base + "/api/runs/" + sourceId)).json();
  let firstPresetRequest = true;
  await page.route("**/api/presets", async (route) => {
    if (firstPresetRequest) {
      firstPresetRequest = false;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "temporary preset service error" }),
      });
    } else await route.continue();
  });
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  await page
    .getByRole("button", { name: "Reload saved version", exact: true })
    .click();
  await page.getByRole("alert").waitFor({ state: "hidden" });
  await page.getByLabel("Saved preset", { exact: true }).waitFor();
  console.log("PASS initial preset-load failure recovers through reload");
  await page.getByRole("heading", { name: "Strategy & risk" }).waitFor();
  const risk = page.getByLabel("Risk per trade (fraction)", { exact: true }),
    fee = page.getByLabel("Fee per share per side (USD)", { exact: true });
  assert(await risk.isDisabled());
  assert.equal(
    await page
      .getByLabel("Saved preset", { exact: true })
      .locator("option")
      .count(),
    5,
  );
  await page
    .getByRole("button", { name: "Duplicate saved preset", exact: true })
    .click();
  await risk.waitFor();
  await page.waitForFunction(
    () => !document.querySelector("fieldset").disabled,
  );
  await risk.fill("0.003");
  assert(
    await page
      .getByRole("button", {
        name: "Apply saved version to new run",
        exact: true,
      })
      .isDisabled(),
  );
  await page.getByRole("button", { name: "Reset draft", exact: true }).click();
  assert.equal(await risk.inputValue(), "0.005");
  console.log("PASS fixed reference, duplicate, draft isolation and reset");
  await page
    .getByLabel("Preset name", { exact: true })
    .fill("Browser cost study");
  await fee.fill("0.01");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved Browser cost study v2. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  assert.equal(
    await page
      .getByLabel("Inspect saved version", { exact: true })
      .inputValue(),
    "2",
  );
  const savedId = await page
    .getByLabel("Saved preset", { exact: true })
    .inputValue();
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + sourceId)).json(),
    old,
  );
  console.log("PASS saved version is durable and does not rewrite source run");
  if (process.env.ORB_SCREENSHOT)
    await page.screenshot({ path: process.env.ORB_SCREENSHOT, fullPage: true });
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(
    (label) =>
      document
        .querySelector('select[aria-label="Selected run"]')
        ?.selectedOptions[0]?.textContent.includes(label),
    "Browser cost study · v2",
  );
  activeRunId = await page.getByLabel("Selected run").inputValue();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByLabel("Saved preset")
    .selectOption({ label: "Browser cost study · v2" });
  const newId = activeRunId;
  assert.notEqual(newId, sourceId);
  const applied = await (await fetch(base + "/api/runs/" + newId)).json();
  assert.equal(applied.applied_settings.preset.version, 2);
  assert.equal(applied.evidence.manifest.config.fee_per_share, "0.01");
  assert.equal(applied.capabilities.order_submission, false);
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + sourceId)).json(),
    old,
  );
  console.log(
    "PASS apply creates a new evaluator-backed run with an immutable preset snapshot",
  );
  await page.reload();
  await simulationView(page);
  await page.getByLabel("Selected run", { exact: true }).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await page.getByLabel("Saved preset", { exact: true }).selectOption(savedId);
  assert.equal(await fee.inputValue(), "0.01");
  await fee.fill("0.02");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved Browser cost study v3. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + newId)).json(),
    applied,
  );
  console.log(
    "PASS saved presets survive browser reload and later edits leave old runs unchanged",
  );
  await page.waitForFunction(
    () =>
      document.querySelector('select[aria-label="Inspect saved version"]')
        .options.length === 3,
  );
  await fee.fill("0.015");
  await page.getByRole("button", { name: "Reset draft", exact: true }).click();
  assert.equal(await fee.inputValue(), "0.02");
  assert.equal(
    await page
      .getByLabel("Inspect saved version", { exact: true })
      .locator("option")
      .count(),
    3,
  );
  await page
    .getByLabel("Inspect saved version", { exact: true })
    .selectOption("1");
  assert.equal(await fee.inputValue(), "0");
  await fee.fill("0.03");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.match(
    await page.getByRole("alert").textContent(),
    /preset changed; reload/,
  );
  await page
    .getByRole("button", { name: "Reload saved version", exact: true })
    .click();
  await page.getByRole("alert").waitFor({ state: "hidden" });
  await page.waitForFunction(
    () =>
      document.querySelector('input[aria-label="Fee per share per side (USD)"]')
        .value === "0.02",
  );
  assert.equal(await fee.inputValue(), "0.02");
  await risk.fill("0");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.match(
    await page.getByRole("alert").textContent(),
    /unsupported settings/,
  );
  assert.equal(
    (await (await fetch(base + "/api/presets")).json()).presets.find(
      (p) => p.id === savedId,
    ).version,
    3,
  );
  console.log(
    "PASS historical versions, stale-edit conflict and server-side validation",
  );
  await page
    .getByRole("button", { name: "Reload saved version", exact: true })
    .click();
  await page.getByRole("alert").waitFor({ state: "hidden" });
  assert.equal(
    await page
      .getByRole("button", { name: "Duplicate saved preset", exact: true })
      .evaluate((e) => getComputedStyle(e).transitionDuration),
    "0s",
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 900 });
    assert(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth + 1,
      ),
      `overflow at ${width}`,
    );
  }
  await page
    .getByLabel("Saved preset", { exact: true })
    .selectOption("orb-mwf");
  assert(await page.getByLabel("Eligible Mon", { exact: true }).isChecked());
  assert(!(await page.getByLabel("Eligible Tue", { exact: true }).isChecked()));
  assert(
    await page
      .getByLabel("Planned target reward / risk (R)", { exact: true })
      .isDisabled(),
  );
  await page
    .getByRole("button", { name: "Duplicate saved preset", exact: true })
    .click();
  const target = page.getByLabel("Planned target reward / risk (R)", {
    exact: true,
  });
  await page.waitForFunction(
    () => !document.querySelector("fieldset").disabled,
  );
  await page
    .getByLabel("Preset name", { exact: true })
    .fill("3R weekday study");
  await target.fill("3");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved 3R weekday study v2. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(
    (label) =>
      document
        .querySelector('select[aria-label="Selected run"]')
        ?.selectedOptions[0]?.textContent.includes(label),
    "3R weekday study · v2",
  );
  activeRunId = await page.getByLabel("Selected run").inputValue();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByLabel("Saved preset")
    .selectOption({ label: "3R weekday study · v2" });
  const policyRunId = activeRunId;
  const policyRun = await (
    await fetch(base + "/api/runs/" + policyRunId)
  ).json();
  assert.deepEqual(
    policyRun.evidence.manifest.config.entry_policy.weekdays,
    [0, 2, 4],
  );
  assert.equal(policyRun.evidence.manifest.config.entry_policy.target_r, "3");
  assert.equal(policyRun.evidence.portfolio.trades[0].reason, "stop_gap");
  assert.match(await page.locator("main").textContent(), /3R weekday study/);
  console.log(
    "PASS MWF duplicate and target R change actual engine outcomes and displayed applied rules",
  );
  for (const d of ["Mon", "Wed", "Fri"])
    await page.getByLabel("Eligible " + d, { exact: true }).uncheck();
  for (const d of ["Tue", "Thu"])
    await page.getByLabel("Eligible " + d, { exact: true }).check();
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved 3R weekday study v3. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(
    (label) =>
      document
        .querySelector('select[aria-label="Selected run"]')
        ?.selectedOptions[0]?.textContent.includes(label),
    "3R weekday study · v3",
  );
  activeRunId = await page.getByLabel("Selected run").inputValue();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByLabel("Saved preset")
    .selectOption({ label: "3R weekday study · v3" });
  const offId = activeRunId;
  const offRun = await (await fetch(base + "/api/runs/" + offId)).json();
  assert.equal(offRun.evidence.summary.candidate_count, 0);
  assert.equal(offRun.evidence.summary.total_return, "0");
  assert.equal(
    offRun.evidence.portfolio.replay.sessions[0].reason,
    "weekday_not_eligible",
  );
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + policyRunId)).json(),
    policyRun,
  );
  console.log(
    "PASS custom entry weekdays block Friday candidates without rewriting previous profile runs",
  );
  for (const d of ["Tue", "Thu"])
    await page.getByLabel("Eligible " + d, { exact: true }).uncheck();
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.match(
    await page.getByRole("alert").textContent(),
    /unsupported settings/,
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 900 });
    assert(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth + 1,
      ),
    );
  }
  if (process.env.ORB_POLICY_SCREENSHOT)
    await page.screenshot({
      path: process.env.ORB_POLICY_SCREENSHOT,
      fullPage: true,
    });
  console.log("PASS empty schedule validation and responsive policy controls");
  // Invented simultaneous SPY/QQQ source: exercise settings via the real UI.
  const spyBars = old.candles.filter((b) => b.symbol === "SPY");
  const bars = [
    ...spyBars,
    ...spyBars.map((b) => ({ ...b, symbol: "QQQ" })),
  ].sort(
    (a, b) =>
      a.start.localeCompare(b.start) || a.symbol.localeCompare(b.symbol),
  );
  const csv =
    "symbol,start,open,high,low,close,volume\n" +
    bars
      .map((b) =>
        [b.symbol, b.start, b.open, b.high, b.low, b.close, b.volume].join(","),
      )
      .join("\n") +
    "\n";
  const imported = await fetch(base + "/api/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "csv",
      source: csv,
      label: "synthetic capacity check",
    }),
  });
  assert.equal(imported.status, 201);
  const capacitySource = (await imported.json()).id;
  await page.reload();
  await simulationView(page);
  await page.getByLabel("Selected run", { exact: true }).waitFor();
  await page
    .getByLabel("Selected run", { exact: true })
    .selectOption(capacitySource);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await page
    .getByLabel("Saved preset", { exact: true })
    .selectOption("orb-reference");
  const entryLimit = page.getByLabel(
    "Maximum filled entries per session (cap)",
    { exact: true },
  );
  const concurrentLimit = page.getByLabel(
    "Maximum concurrent positions (pending included)",
    { exact: true },
  );
  assert(await entryLimit.isDisabled());
  await page
    .getByRole("button", { name: "Duplicate saved preset", exact: true })
    .click();
  await page.waitForFunction(
    () => !document.querySelector("fieldset").disabled,
  );
  await page.getByLabel("Preset name", { exact: true }).fill("One concurrent");
  await concurrentLimit.fill("1");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved One concurrent v2. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(
    (label) =>
      document
        .querySelector('select[aria-label="Selected run"]')
        ?.selectedOptions[0]?.textContent.includes(label),
    "One concurrent · v2",
  );
  activeRunId = await page.getByLabel("Selected run").inputValue();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByLabel("Saved preset")
    .selectOption({ label: "One concurrent · v2" });
  const cappedId = activeRunId;
  const cappedRun = await (await fetch(base + "/api/runs/" + cappedId)).json();
  assert.equal(
    cappedRun.evidence.summary.trade_reasons.concurrent_position_limit,
    1,
  );
  assert.equal(
    cappedRun.applied_settings.preset.settings.max_concurrent_positions,
    "1",
  );
  assert.match(await page.locator("main").textContent(), /One concurrent/);
  console.log(
    "PASS saved concurrency cap rejects actual simultaneous candidate and displays applied limits",
  );
  await entryLimit.fill("1.5");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.equal(
    await page
      .getByLabel("Inspect saved version", { exact: true })
      .inputValue(),
    "2",
  );
  await page.getByRole("button", { name: "Reset draft", exact: true }).click();
  await entryLimit.fill("1");
  await concurrentLimit.fill("2");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved One concurrent v3. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(
    (label) =>
      document
        .querySelector('select[aria-label="Selected run"]')
        ?.selectedOptions[0]?.textContent.includes(label),
    "One concurrent · v3",
  );
  activeRunId = await page.getByLabel("Selected run").inputValue();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByLabel("Saved preset")
    .selectOption({ label: "One concurrent · v3" });
  const countId = activeRunId;
  const countRun = await (await fetch(base + "/api/runs/" + countId)).json();
  assert.equal(countRun.evidence.summary.trade_reasons.session_entry_limit, 1);
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + cappedId)).json(),
    cappedRun,
  );
  assert.equal(
    countRun.evidence.manifest.base_schema_version,
    "orb-evaluation-v7",
  );
  console.log(
    "PASS integer validation, reset and session cap application preserve earlier run versions",
  );
  // Exercise the v4 controls through save, validation, evaluation and old history.
  await page
    .getByLabel("Opening range (minutes)", { exact: true })
    .selectOption("30");
  await page
    .getByLabel("Entry delay after range (minutes)", { exact: true })
    .selectOption("30");
  await page
    .getByLabel("Entry cutoff (New York)", { exact: true })
    .selectOption("10:00");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.equal(
    await page
      .getByLabel("Inspect saved version", { exact: true })
      .inputValue(),
    "3",
  );
  await page.getByRole("button", { name: "Reset draft", exact: true }).click();
  assert.equal(
    await page
      .getByLabel("Opening range (minutes)", { exact: true })
      .inputValue(),
    "15",
  );
  console.log(
    "PASS empty custom entry window is rejected and reset restores saved timing",
  );
  await page
    .getByLabel("Preset name", { exact: true })
    .fill("Timing stop study");
  await page
    .getByLabel("Opening range (minutes)", { exact: true })
    .selectOption("5");
  await page
    .getByLabel("Entry delay after range (minutes)", { exact: true })
    .selectOption("5");
  await page
    .getByLabel("Entry cutoff (New York)", { exact: true })
    .selectOption("10:30");
  await page
    .getByLabel("Initial stop policy", { exact: true })
    .selectOption("range_low");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved Timing stop study v4. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.waitForFunction(() =>
    document
      .querySelector('select[aria-label="Selected run"]')
      ?.selectedOptions[0]?.textContent.includes("Timing stop study · v4"),
  );
  const timingId = await page.getByLabel("Selected run").inputValue();
  const timingRun = await (await fetch(base + "/api/runs/" + timingId)).json();
  const appliedPolicy = timingRun.evidence.manifest.config.entry_policy;
  assert.equal(appliedPolicy.schema_version, "orb-entry-policy-v2");
  assert.equal(appliedPolicy.range_minutes, "5");
  assert.equal(appliedPolicy.entry_delay_minutes, "5");
  assert.equal(appliedPolicy.entry_cutoff_local, "10:30");
  assert.equal(appliedPolicy.stop_policy, "range_low");
  assert.equal(
    timingRun.applied_settings.preset.settings.schema_version,
    "orb-research-settings-v4",
  );
  const filledTrade = timingRun.decisions.find((d) => d.trade.entry_at).trade;
  assert.equal(
    filledTrade.stop,
    timingRun.opening_ranges.find((r) => r.symbol === filledTrade.symbol).low,
  );
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + countId)).json(),
    countRun,
  );
  console.log(
    "PASS timing and range-low controls drive real engine trades with immutable applied v4 settings",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByLabel("Inspect saved version", { exact: true })
    .selectOption("3");
  assert.equal(
    await page
      .getByLabel("Opening range (minutes)", { exact: true })
      .inputValue(),
    "15",
  );
  assert.equal(
    await page.getByLabel("Initial stop policy", { exact: true }).inputValue(),
    "range_midpoint",
  );
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + timingId)).json(),
    timingRun,
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 1000 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
  }
  console.log(
    "PASS historical preset timing remains unchanged and new controls reflow",
  );
  assert.deepEqual(errors, []);
  console.log("PASS preset reflow, reduced motion and no browser errors");
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    const ended = new Promise((r) => server.once("exit", r));
    server.kill();
    await ended;
  }
  await rm(temp, { recursive: true, force: true });
}
