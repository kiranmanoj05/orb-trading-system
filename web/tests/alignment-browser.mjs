/** Real source-backed study requests; invented fixtures and no broker orders. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { researchFixtures, simulationView } from "./research-view.mjs";
const root = fileURLToPath(new URL("../..", import.meta.url)),
  temp = await mkdtemp(tmpdir() + "/orb-alignment-"),
  base = "http://127.0.0.1:8891";
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  [
    "-m",
    "orb.dashboard_api",
    "--examples",
    "--port",
    "8891",
    "--preset-db",
    temp + "/presets.sqlite",
    "--research-db",
    temp + "/research.sqlite",
    "--paper-db",
    temp + "/paper.sqlite",
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
server.stderr.on("data", (x) => (logs += x));
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
  const runs = await (await fetch(base + "/api/runs")).json(),
    complete = runs.find((r) => r.label.endsWith("complete_trade"));
  const original = await (
    await fetch(base + "/api/runs/" + complete.id)
  ).json();
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
    }),
    errors = [],
    actions = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.method() !== "GET" && r.url().includes("/api/paper/"))
      actions.push(r.url());
  });
  await researchFixtures(page);
  await page.goto(base);
  await simulationView(page);
  await page
    .getByLabel("Selected run", { exact: true })
    .selectOption(complete.id);
  await page.getByRole("button", { name: "Details", exact: true }).click();
  await page.getByText("SPY paper-preset study", { exact: true }).click();
  await page
    .getByLabel("Study paper preset", { exact: true })
    .selectOption("paper-balanced");
  const response = page.waitForResponse((r) =>
    r.url().endsWith("/paper-alignment"),
  );
  await page
    .getByRole("button", { name: "Evaluate SPY study", exact: true })
    .click();
  const body = await (await response).json();
  await page
    .getByRole("button", { name: "Download SPY study JSON", exact: true })
    .waitFor();
  assert.equal(body.preset.id, "paper-balanced");
  assert.equal(body.preset.version, 1);
  assert.deepEqual(body.evaluated_symbols, ["SPY"]);
  assert(body.trials.every((r) => r.trades.every((t) => t.symbol === "SPY")));
  assert.equal(
    body.source_dataset.sha256,
    original.evidence.manifest.dataset.sha256,
  );
  assert.equal(body.broker_execution_verified, false);
  assert.match(
    await page.locator(".paper-alignment").innerText(),
    /selected run and chart keep their original settings/,
  );
  console.log(
    "PASS real study uses frozen paper preset and SPY scope without changing original source",
  );
  const downloaded = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download SPY study JSON", exact: true })
    .click();
  const file = await downloaded,
    report = JSON.parse(await readFile(await file.path(), "utf8"));
  assert.deepEqual(report, body);
  assert(report.synthetic);
  assert(!report.profitability_established);
  assert(
    report.trials[1].report.config.fee_per_share !==
      report.trials[0].report.config.fee_per_share,
  );
  assert.deepEqual(
    await (await fetch(base + "/api/runs/" + complete.id)).json(),
    original,
  );
  assert.deepEqual(actions, []);
  console.log(
    "PASS downloadable study retains all rerun cost cells and explicit non-broker boundaries",
  );
  await page
    .getByLabel("Study paper preset", { exact: true })
    .selectOption("orb-reference");
  assert.equal(
    await page
      .getByRole("button", { name: "Download SPY study JSON", exact: true })
      .count(),
    0,
  );
  await page
    .getByRole("button", { name: "Evaluate SPY study", exact: true })
    .click();
  await page.locator(".paper-alignment [role=alert]").waitFor();
  assert.match(await page.locator(".paper-alignment").innerText(), /Paper v1/);
  console.log(
    "PASS incompatible settings are explicitly rejected and previous results disappear",
  );
  await page
    .getByLabel("Study paper preset", { exact: true })
    .selectOption("paper-balanced");
  let release, arrive;
  const gate = new Promise((r) => (release = r)),
    arrived = new Promise((r) => (arrive = r));
  await page.route("**/paper-alignment", async (route) => {
    const result = await route.fetch();
    arrive();
    await gate;
    await route.fulfill({ response: result });
  });
  await page
    .getByRole("button", { name: "Evaluate SPY study", exact: true })
    .click();
  await arrived;
  await page
    .getByLabel("Study paper preset", { exact: true })
    .selectOption("paper-calm");
  const oldResponse = page.waitForResponse((r) =>
    r.url().endsWith("/paper-alignment"),
  );
  release();
  await oldResponse;
  await page.evaluate(
    () =>
      new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
  );
  assert.equal(
    await page
      .getByRole("button", { name: "Download SPY study JSON", exact: true })
      .count(),
    0,
  );
  await page.unroute("**/paper-alignment");
  console.log(
    "PASS delayed result cannot overwrite a newly selected study preset",
  );
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await page
    .getByLabel("Selected run", { exact: true })
    .selectOption(runs.find((r) => r.label.endsWith("missing_entry")).id);
  await page.getByRole("button", { name: "Details", exact: true }).click();
  await page.getByText("SPY paper-preset study", { exact: true }).click();
  await page
    .getByRole("button", { name: "Evaluate SPY study", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Download SPY study JSON", exact: true })
    .waitFor();
  const text = await page.locator(".paper-alignment").innerText();
  assert.match(text, /incomplete/);
  assert.match(text, /Unavailable/);
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 1050 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(actions, []);
  console.log(
    "PASS missing SPY evidence stays unavailable with responsive layout and no paper actions",
  );
} finally {
  if (browser) await browser.close();
  server.kill();
  if (server.exitCode === null)
    await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
