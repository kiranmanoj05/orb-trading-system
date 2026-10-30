import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = fileURLToPath(new URL("../..", import.meta.url));
const port = 8877,
  base = `http://127.0.0.1:${port}`;
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  [
    "-m",
    "orb.dashboard_api",
    "--examples",
    "--port",
    String(port),
    "--web-dir",
    root + "/web/build",
  ],
  { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
);
let logs = "";
server.stdout.on("data", (b) => (logs += b));
server.stderr.on("data", (b) => (logs += b));
let browser;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error(logs);
    try {
      const r = await fetch(base + "/api/runs");
      if (r.ok) {
        ready = true;
        break;
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  assert(ready, "API startup timed out: " + logs);
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
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto(base);
  await page.getByText("Cash at end", { exact: true }).waitFor();
  assert.equal(await page.getByRole("img").count(), 2);
  assert.match(
    await page.locator(".notice").textContent(),
    /SYNTHETIC EXAMPLE/,
  );
  console.log("PASS overview is backed by synthetic engine evidence");
  await page
    .getByRole("button", { name: "Show full session", exact: true })
    .click();
  assert.equal(
    await page.getByRole("button", { name: "Focus opening / trade" }).count(),
    1,
  );
  await page.getByRole("checkbox", { name: "Levels", exact: true }).uncheck();
  await page.getByText(/Accessible candle table/).click();
  assert.equal(await page.locator("tbody tr").count(), 390);
  console.log("PASS chart zoom, levels and accessible candle table");
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Decisions", exact: false })
    .click();
  await page.getByRole("button", { name: /SPY.*closed.*target/ }).click();
  assert.match(
    await page.locator(".inset").first().textContent(),
    /sized:position cap/,
  );
  await page.getByText("Exact events & availability times").click();
  assert.match(
    await page.locator("pre").first().textContent(),
    /"planned_risk": "31.2"/,
  );
  console.log("PASS decision links chart and exact sizing evidence");
  const listing = await (await fetch(base + "/api/runs")).json();
  await page
    .getByLabel("Selected run")
    .selectOption(listing.find((r) => r.label.endsWith("known_skip")).id);
  await page.getByText("known no entry", { exact: true }).first().waitFor();
  assert.match(await page.locator(".stat").first().textContent(), /0.00%/);
  await page
    .getByLabel("Selected run")
    .selectOption(listing.find((r) => r.label.endsWith("missing_entry")).id);
  await page.getByText("uncertain entry", { exact: true }).first().waitFor();
  assert.match(
    await page.locator(".stat").first().textContent(),
    /Unavailable/,
  );
  assert.match(
    await page.locator(".notice").textContent(),
    /aggregate return and drawdown withheld/,
  );
  console.log("PASS known skip vs incomplete outcomes stay distinct");
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Applied settings", exact: false })
    .click();
  await page.getByText("Configuration fingerprint & exact values").click();
  assert.match(await page.locator("pre").textContent(), /"target_r": "2"/);
  assert.equal(await page.locator('main input[type="range"]').count(), 0);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Data & health", exact: false })
    .click();
  await page
    .getByRole("heading", { name: "Coverage by symbol and session" })
    .waitFor();
  assert.match(await page.locator("table").textContent(), /389 \/ 390/);
  assert.match(
    await page.locator("main").textContent(),
    /Not applicable · historical simulation/,
  );
  console.log("PASS immutable settings and explicit historical health");
  await page.getByRole("checkbox", { name: "Reduce transparency" }).check();
  assert.equal(
    await page
      .locator(".sidebar")
      .evaluate((e) => getComputedStyle(e).backdropFilter),
    "none",
  );
  await page.reload();
  await page.getByText("Cash at end", { exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("checkbox", { name: "Reduce transparency" })
      .isChecked(),
    true,
  );
  console.log("PASS reduced transparency persists, reduced motion honoured");
  await page.getByRole("button", { name: "+ Import run" }).click();
  await page.getByRole("dialog").waitFor();
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("dialog").count(), 0);
  await page.getByRole("button", { name: "+ Import run" }).click();
  await page
    .getByLabel("Run label", { exact: true })
    .fill("Browser import <b>plain text</b>");
  await page
    .getByLabel("Source CSV", { exact: true })
    .setInputFiles(root + "/fixtures/orb_execution_sample.csv");
  const report = await (
    await fetch(base + "/api/runs/" + listing[0].id)
  ).json();
  await page
    .getByLabel("Saved evaluation JSON (optional, v4–v7)")
    .setInputFiles({
      name: "mismatch.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(report.evidence)),
    });
  if (process.env.ORB_IMPORT_DIAGNOSTICS) {
    console.log(
      await page
        .getByRole("button", { name: "Validate & import" })
        .evaluate((e) => ({
          rect: e.getBoundingClientRect().toJSON(),
          visible: getComputedStyle(e).visibility,
          dialog: e.closest("dialog").open,
        })),
    );
    await page.screenshot({
      path: process.env.ORB_IMPORT_DIAGNOSTICS,
      fullPage: true,
    });
  }
  await page.getByRole("button", { name: "Validate & import" }).click();
  await page.getByRole("alert").waitFor();
  assert.equal((await (await fetch(base + "/api/runs")).json()).length, 3);
  await page
    .getByLabel("Saved evaluation JSON (optional, v4–v7)")
    .setInputFiles([]);
  await page.getByRole("button", { name: "Validate & import" }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page.getByText("Cash at end", { exact: true }).waitFor();
  assert.match(
    await page
      .getByLabel("Selected run")
      .locator("option:checked")
      .textContent(),
    /Browser import <b>plain text<\/b>/,
  );
  assert.equal(await page.locator(".run-picker b").count(), 0);
  console.log(
    "PASS modal keyboard, rejected mismatch, working source import and escaped label",
  );
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export dashboard evidence" }).click();
  const file = await download;
  const exported = JSON.parse(await readFile(await file.path(), "utf8"));
  assert.equal(exported.mode, "historical_simulation");
  assert.equal(exported.capabilities.order_submission, false);
  console.log("PASS evidence download retains mode and capability limits");
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 900 });
    assert(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth + 1,
      ),
      `document overflows at ${width}px`,
    );
  }
  console.log("PASS mobile, tablet and desktop document reflow");
  assert.deepEqual(
    errors.filter((e) => !e.includes("422 (Unprocessable Entity)")),
    [],
  );
  console.log("PASS no unexpected browser errors");
  if (process.env.ORB_SCREENSHOT) {
    await page.getByLabel("Selected run").selectOption(listing[0].id);
    await page.getByText("Cash at end", { exact: true }).waitFor();
    await page.getByRole("checkbox", { name: "Reduce transparency" }).uncheck();
    await page.screenshot({ path: process.env.ORB_SCREENSHOT, fullPage: true });
  }
} finally {
  if (browser) await browser.close();
  server.kill();
}
