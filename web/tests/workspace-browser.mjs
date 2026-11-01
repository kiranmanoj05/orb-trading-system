/** Local full-stack regression runner for the redesigned workspace. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url)),
  temp = await mkdtemp(join(tmpdir(), "orb-workspace-")),
  port = 8877,
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
server.stderr.on("data", (b) => (logs += b));
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
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
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_EXECUTABLE_PATH
      ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH }
      : {}),
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({
      viewport: { width: 1500, height: 1000 },
      reducedMotion: "reduce",
    }),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base);
  await page.getByLabel("Selected run").waitFor();
  assert.equal(
    await page.getByRole("navigation").getByRole("button").count(),
    2,
  );
  assert.equal(await page.locator(".sidebar").count(), 0);
  assert.match(await page.locator(".account h1").textContent(), /10,062.40/);
  console.log("PASS engine equity and minimal navigation");
  await page.getByRole("button", { name: "Expand chart", exact: true }).click();
  await page.getByRole("button", { name: "Focus trade", exact: true }).click();
  await page.getByRole("button", { name: "Full session", exact: true }).click();
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "Expand chart", exact: true })
    .waitFor();
  await page.getByText(/Candle data/).click();
  assert.equal(await page.locator("tbody tr").count(), 390);
  console.log(
    "PASS chart expansion, focus, session and accessible candle data",
  );
  await page.getByRole("button", { name: /^Trades/ }).click();
  await page.getByText("target", { exact: true }).click();
  assert.match(await page.locator(".trade-detail").textContent(), /100.30/);
  await page.getByRole("button", { name: /SPY · closed/ }).click();
  console.log("PASS engine trade details and chart navigation");
  const runs = await (await fetch(base + "/api/runs")).json();
  await page
    .getByLabel("Selected run")
    .selectOption(runs.find((r) => r.label.endsWith("missing_entry")).id);
  await page.getByRole("button", { name: /Incomplete evidence/ }).waitFor();
  await page
    .getByRole("button", { name: "View balance ↗", exact: true })
    .click();
  assert.match(await page.locator("dialog").textContent(), /Unavailable/);
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  console.log("PASS incomplete results do not fabricate metrics");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Saved preset").waitFor();
  await page
    .getByRole("button", { name: "Duplicate saved preset", exact: true })
    .click();
  await page.getByLabel("Preset name").fill("Browser regression variant");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page.getByText(/Saved Browser regression variant/).waitFor();
  await page
    .getByRole("button", {
      name: "Apply saved version to new run",
      exact: true,
    })
    .click();
  await page.getByRole("button", { name: "Workspace", exact: true }).waitFor();
  console.log("PASS validated preset duplicate/save/apply");
  await page
    .getByRole("button", { name: "Data & Alpaca ↗", exact: true })
    .click();
  assert(
    await page
      .getByRole("button", { name: "Refresh paper account", exact: true })
      .isDisabled(),
  );
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  console.log("PASS absent backend credentials are explicit");
  await page.getByRole("button", { name: "Appearance", exact: true }).click();
  await page
    .locator("input[type=file]")
    .setInputFiles({
      name: "original.png",
      mimeType: "image/png",
      buffer: await readFile(root + "/web/public/wallpaper-poster.png"),
    });
  await page.getByText("Your saved image ↗", { exact: true }).waitFor();
  await page.reload();
  await page.getByRole("button", { name: "Appearance", exact: true }).click();
  await page.getByText("Your saved image ↗", { exact: true }).waitFor();
  await page
    .getByRole("button", { name: "Close appearance", exact: true })
    .click();
  console.log("PASS original image persists across reload");
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 1000 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
  }
  assert.deepEqual(errors, []);
  console.log("PASS responsive layout and no page errors");
} finally {
  if (browser) await browser.close();
  server.kill();
  if (server.exitCode === null)
    await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
