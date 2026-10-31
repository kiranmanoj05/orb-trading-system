import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url)),
  temp = await mkdtemp(join(tmpdir(), "orb-comparison-")),
  base = "http://127.0.0.1:8882";
let server,
  browser,
  logs = "";
async function start(examples = false) {
  server = spawn(
    process.env.ORB_PYTHON || "python3",
    [
      "-m",
      "orb.dashboard_api",
      ...(examples ? ["--examples"] : []),
      "--port",
      "8882",
      "--web-dir",
      root + "/web/build",
      "--preset-db",
      temp + "/p.sqlite",
      "--research-db",
      temp + "/r.sqlite",
    ],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );
  server.stderr.on("data", (b) => (logs += b));
  for (let i = 0; i < 150; i++) {
    if (server.exitCode !== null) throw Error(logs);
    try {
      if ((await fetch(base + "/api/runs")).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw Error(logs);
}
async function stop() {
  const p = new Promise((r) => server.once("exit", r));
  server.kill();
  await p;
  server = null;
}
const get = async (path) => (await fetch(base + path)).json();
try {
  await start(true);
  const examples = await get("/api/runs"),
    example = await get("/api/runs/" + examples[0].id);
  const bars = [0, 3, 4].flatMap((days) =>
    example.candles.map((b) => ({
      ...b,
      start: new Date(
        new Date(b.start).getTime() + days * 86400000,
      ).toISOString(),
    })),
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
      label: "Invented three-session comparison",
    }),
  });
  assert.equal(imported.status, 201);
  const source = (await imported.json()).id,
    original = await get("/api/runs/" + source);
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
  await page.getByLabel("Selected run", { exact: true }).selectOption(source);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Comparisons", exact: false })
    .click();
  await page
    .getByRole("heading", { name: "Compare the same evidence.", exact: true })
    .waitFor();
  await page
    .getByLabel("Comparison name", { exact: true })
    .fill("Browser schedule study");
  await page
    .getByLabel("Validation begins (NY exchange session)", { exact: true })
    .fill("2026-10-13");
  await page
    .getByRole("button", { name: "Queue comparison protocol", exact: true })
    .click();
  await page.getByText(/Comparison job .* queued/).waitFor();
  const queued = (await get("/api/jobs")).find(
    (j) => j.request.kind === "comparison",
  );
  assert(queued);
  assert.equal(queued.request.plan.presets.length, 2);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Research jobs/ })
    .click();
  await page
    .getByRole("button", { name: "Run next queued job", exact: true })
    .click();
  await page.getByText("Job succeeded.", { exact: true }).waitFor();
  const done = (await get("/api/jobs")).find((j) => j.id === queued.id);
  assert(done.comparison_id);
  await page
    .getByRole("button", {
      name: "Open comparison " + done.comparison_id.slice(0, 8),
      exact: true,
    })
    .click();
  await page
    .getByRole("heading", { name: "Browser schedule study", exact: true })
    .waitFor();
  assert.equal(await page.locator("table tbody tr").count(), 8);
  assert.match(
    await page.locator("main").textContent(),
    /Retrospective, unsealed validation/,
  );
  const result = await get("/api/comparisons/" + done.comparison_id);
  const inactive = result.rows.find(
    (r) =>
      r.period === "validation" &&
      r.preset_id === "orb-mwf" &&
      r.cost_name === "Zero diagnostic",
  );
  assert.equal(inactive.metrics.total_return_fraction, "0");
  assert.equal(inactive.metrics.expectancy_net_r, null);
  assert.deepEqual(await get("/api/runs/" + source), original);
  console.log(
    "PASS UI queues frozen schedule/cost protocol, real worker produces eight honest cells and source stays immutable",
  );
  await page
    .getByRole("button", { name: "Inspect trial 6", exact: true })
    .click();
  await page.getByText("Cash at end", { exact: true }).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Applied settings/ })
    .click();
  await page
    .getByRole("heading", {
      name: "Comparison trial effective settings",
      exact: true,
    })
    .waitFor();
  assert.match(
    await page.locator("main").textContent(),
    /cost override Zero diagnostic/,
  );
  console.log(
    "PASS trial navigation shows effective settings and identifies base preset plus cost override",
  );
  await stop();
  await start();
  await page.reload();
  await page.getByText("Cash at end", { exact: true }).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Comparisons", exact: false })
    .click();
  await page
    .getByLabel("Saved comparison", { exact: true })
    .selectOption(done.comparison_id);
  await page
    .getByRole("heading", { name: "Browser schedule study", exact: true })
    .waitFor();
  assert.deepEqual(await get("/api/comparisons/" + done.comparison_id), result);
  const report = await fetch(
    base + "/api/comparisons/" + done.comparison_id + "/report",
  );
  assert.equal(report.status, 200);
  assert.match(await report.text(), /Return \(fraction\)/);
  const dl = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Export comparison JSON", exact: true })
    .click();
  assert.match((await dl).suggestedFilename(), /^orb-comparison/);
  console.log(
    "PASS saved comparison survives actual restart with working JSON and Markdown exports",
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 900 });
    const overflow = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
      elements: [...document.querySelectorAll("main *")]
        .filter(
          (e) =>
            e.getBoundingClientRect().right >
            document.documentElement.clientWidth + 2,
        )
        .slice(0, 15)
        .map((e) => ({
          tag: e.tagName,
          cls: e.className,
          text: e.textContent?.slice(0, 60),
        })),
    }));
    if (overflow.scroll > overflow.width + 1) console.log(overflow);
    assert(overflow.scroll <= overflow.width + 1);
  }
  await page.getByLabel("Selected run", { exact: true }).selectOption(source);
  await page
    .getByLabel("Validation begins (NY exchange session)", { exact: true })
    .fill("2026-10-10");
  await page
    .getByRole("button", { name: "Queue comparison protocol", exact: true })
    .click();
  await page.getByRole("alert").waitFor();
  assert.equal(
    (await get("/api/jobs")).filter((j) => j.request.kind === "comparison")
      .length,
    1,
  );
  assert.deepEqual(errors, []);
  if (process.env.ORB_COMPARISON_SCREENSHOT)
    await page.screenshot({
      path: process.env.ORB_COMPARISON_SCREENSHOT,
      fullPage: true,
    });
  console.log(
    "PASS invalid non-session split is rejected, no extra job, responsive table/reduced-motion controls and no browser errors",
  );
} finally {
  if (browser) await browser.close();
  if (server) await stop();
  await rm(temp, { recursive: true, force: true });
}
