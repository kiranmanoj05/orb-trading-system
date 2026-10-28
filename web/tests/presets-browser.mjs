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
    "--preset-db",
    temp + "/presets.sqlite",
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
  const page = await browser.newPage({
      viewport: { width: 1500, height: 1000 },
      reducedMotion: "reduce",
    }),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base);
  await page.getByText("Cash at end", { exact: true }).waitFor();
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
    .getByRole("button", { name: /Applied settings/ })
    .click();
  await page.getByRole("alert").waitFor();
  await page
    .getByRole("button", { name: "Reload saved version", exact: true })
    .click();
  await page.getByRole("alert").waitFor({ state: "hidden" });
  await page.getByLabel("Saved preset", { exact: true }).waitFor();
  console.log("PASS initial preset-load failure recovers through reload");
  await page
    .getByRole("heading", { name: "Build a deliberate experiment." })
    .waitFor();
  const risk = page.getByLabel("Risk per trade (fraction)", { exact: true }),
    fee = page.getByLabel("Fee per share per side (USD)", { exact: true });
  assert(await risk.isDisabled());
  assert.equal(
    await page
      .getByLabel("Saved preset", { exact: true })
      .locator("option")
      .count(),
    2,
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
  await page
    .getByRole("heading", { name: "Browser cost study · v2", exact: true })
    .waitFor();
  const newId = await page
    .getByLabel("Selected run", { exact: true })
    .inputValue();
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
  await page.getByText("Cash at end", { exact: true }).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Applied settings/ })
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
  await page
    .getByRole("heading", { name: "3R weekday study · v2", exact: true })
    .waitFor();
  const policyRunId = await page
    .getByLabel("Selected run", { exact: true })
    .inputValue();
  const policyRun = await (
    await fetch(base + "/api/runs/" + policyRunId)
  ).json();
  assert.deepEqual(
    policyRun.evidence.manifest.config.entry_policy.weekdays,
    [0, 2, 4],
  );
  assert.equal(policyRun.evidence.manifest.config.entry_policy.target_r, "3");
  assert.equal(policyRun.evidence.portfolio.trades[0].reason, "stop_gap");
  assert.match(
    await page.locator("main").textContent(),
    /Range midpoint \/ 3R/,
  );
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
  await page
    .getByRole("heading", { name: "3R weekday study · v3", exact: true })
    .waitFor();
  const offId = await page
    .getByLabel("Selected run", { exact: true })
    .inputValue();
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
  assert.deepEqual(errors, []);
  console.log("PASS preset reflow, reduced motion and no browser errors");
} finally {
  if (browser) await browser.close();
  server.kill();
  await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
