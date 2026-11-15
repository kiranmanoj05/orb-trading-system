/** Full-stack report flows; invented fixture runs, no broker/credentials/orders. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { researchFixtures, simulationView } from "./research-view.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const temp = await mkdtemp(tmpdir() + "/orb-performance-"),
  base = "http://127.0.0.1:8890";
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  [
    "-m",
    "orb.dashboard_api",
    "--examples",
    "--port",
    "8890",
    "--paper-db",
    temp + "/paper.sqlite",
    "--preset-db",
    temp + "/presets.sqlite",
    "--research-db",
    temp + "/research.sqlite",
    "--data-dir",
    temp + "/data",
  ],
  {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, APCA_API_KEY_ID: "", APCA_API_SECRET_KEY: "" },
  },
);
let logs = "",
  browser;
server.stderr.on("data", (v) => (logs += v));
try {
  let ready = false;
  for (let i = 0; i < 120; i++) {
    if (server.exitCode !== null) throw Error(logs);
    try {
      if ((await fetch(base + "/api/runs")).ok) {
        ready = true;
        break;
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  assert(ready, logs);
  const runs = await (await fetch(base + "/api/runs")).json();
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.BROWSER_EXECUTABLE_PATH || undefined,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--single-process",
      "--no-zygote",
    ],
  });
  const page = await browser.newPage({
    viewport: { width: 1500, height: 1050 },
    reducedMotion: "reduce",
  });
  await page.clock.install();
  const paperStatus = await (await fetch(base + "/api/paper/status")).json();
  await page.route("**/api/paper/status", (route) =>
    route.fulfill({ json: paperStatus }),
  );
  const errors = [],
    paperWrites = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.method() !== "GET" && r.url().includes("/api/paper/"))
      paperWrites.push(r.url());
  });
  await researchFixtures(page);
  await page.goto(base);
  await page.getByRole("button", { name: /^Trades / }).click();
  await page.getByText("Paper cost estimates", { exact: true }).click();
  await page
    .getByRole("button", { name: "Calculate report", exact: true })
    .click();
  await page.getByText("Estimated net P&L (USD)", { exact: true }).waitFor();
  const panel = page.locator(".performance-report");
  assert.match(await panel.innerText(), /Broker net P&L\s+Unavailable/);
  assert.match(
    await panel.innerText(),
    /Estimated net P&L \(USD\)\s+Unavailable/,
  );
  assert.deepEqual(paperWrites, []);
  console.log(
    "PASS empty paper report withholds net results and sends no broker action",
  );
  paperStatus.evidence.entries_blocked = true;
  await page.clock.runFor(15000);
  await page.getByText(/Broker correction hold:/).waitFor();
  assert.equal(
    await page.getByText("Estimated net P&L (USD)", { exact: true }).count(),
    0,
  );
  console.log(
    "PASS new evidence invalidates an already displayed paper estimate",
  );
  paperStatus.evidence.entries_blocked = false;
  await page.clock.runFor(15000);
  await page.getByText(/Broker correction hold:/).waitFor({ state: "hidden" });
  const oldReport = await (await fetch(base + "/api/paper/performance")).json();
  let releaseReport, requestArrived;
  const pending = new Promise((resolve) => {
    releaseReport = resolve;
  });
  const arrived = new Promise((resolve) => {
    requestArrived = resolve;
  });
  await page.route("**/api/paper/performance", async (route) => {
    requestArrived();
    await pending;
    await route.fulfill({ json: oldReport });
  });
  await page
    .getByRole("button", { name: "Calculate report", exact: true })
    .click();
  await arrived;
  paperStatus.evidence.entries_blocked = true;
  await page.clock.runFor(15000);
  await page.getByText(/Broker correction hold:/).waitFor();
  const staleResponse = page.waitForResponse((r) =>
    r.url().endsWith("/api/paper/performance"),
  );
  releaseReport();
  await staleResponse;
  // A later UI turn after response parsing must still have no stale estimate.
  await page
    .getByRole("button", { name: "Calculate report", exact: true })
    .waitFor();
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  assert.equal(
    await page.getByText("Estimated net P&L (USD)", { exact: true }).count(),
    0,
  );
  await page.unroute("**/api/paper/performance");
  console.log(
    "PASS an older in-flight report cannot reappear after evidence changes",
  );
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await simulationView(page);
  const complete = runs.find((r) => r.label.endsWith("complete_trade"));
  await page
    .getByLabel("Selected run", { exact: true })
    .selectOption(complete.id);
  await page.getByRole("button", { name: "Details", exact: true }).click();
  await page.getByText("Performance report", { exact: true }).click();
  await page
    .getByRole("button", { name: "Calculate report", exact: true })
    .click();
  await page.getByText("Modelled net P&L (USD)", { exact: true }).waitFor();
  assert.match(await panel.innerText(), /insufficient sessions/);
  assert.match(await panel.innerText(), /Profitability is not established/);
  const downloaded = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download report JSON", exact: true })
    .click();
  const file = await downloaded;
  const report = JSON.parse(await readFile(await file.path(), "utf8"));
  assert.equal(report.evaluation_id, complete.id);
  assert.equal(report.synthetic, true);
  assert.equal(report.profitability_established, false);
  console.log(
    "PASS historical report and JSON retain synthetic identity and small-sample uncertainty",
  );
  const markdownDownload = page.waitForEvent("download");
  await page
    .getByRole("link", { name: "Download readable report", exact: true })
    .click();
  const markdown = await markdownDownload;
  assert.match(
    await readFile(await markdown.path(), "utf8"),
    /Synthetic: True/,
  );
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  const missing = runs.find((r) => r.label.endsWith("missing_entry"));
  await page
    .getByLabel("Selected run", { exact: true })
    .selectOption(missing.id);
  await page.getByRole("button", { name: "Details", exact: true }).click();
  await page.getByText("Performance report", { exact: true }).click();
  assert.equal(
    await page.getByText("Modelled net P&L (USD)", { exact: true }).count(),
    0,
  );
  await page
    .getByRole("button", { name: "Calculate report", exact: true })
    .click();
  await page.getByText("Modelled net P&L (USD)", { exact: true }).waitFor();
  assert.match(
    await panel.innerText(),
    /Modelled net P&L \(USD\)\s+Unavailable/,
  );
  console.log(
    "PASS source switching clears old results and incomplete evidence withholds aggregate profit",
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 1050 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(paperWrites, []);
  console.log(
    "PASS responsive reports and Markdown download without JavaScript errors or paper actions",
  );
} finally {
  if (browser) await browser.close();
  server.kill();
  if (server.exitCode === null)
    await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
