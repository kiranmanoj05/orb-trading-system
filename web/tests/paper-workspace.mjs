/** Paper/simulation isolation and explicit Start/Stop with injected broker data. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url));
const temp = await mkdtemp(tmpdir() + "/orb-paper-ui-"),
  base = "http://127.0.0.1:8884";
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  [
    "-m",
    "orb.dashboard_api",
    "--examples",
    "--port",
    "8884",
    "--preset-db",
    temp + "/p.sqlite",
    "--research-db",
    temp + "/r.sqlite",
    "--paper-db",
    temp + "/paper.sqlite",
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
const get = async (p) => (await fetch(base + p)).json();
try {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(base + "/api/runs")).ok) break;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  const rows = await get("/api/runs"),
    source = await get("/api/runs/" + rows.find((r) => r.complete).id);
  browser = await chromium.launch({
    executablePath: process.env.BROWSER_EXECUTABLE_PATH || undefined,
    headless: true,
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
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => {
    window.chartText = [];
    const f = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (t, ...a) {
      window.chartText.push(String(t));
      return f.call(this, t, ...a);
    };
  });
  let running = false,
    market = null,
    posts = [];
  let evidence = {
    attempts: [],
    orders: [],
    holds: [],
    entries_blocked: false,
  };
  const state = () => ({
    evidence,
    available: true,
    enabled: running,
    preset: running ? { name: "Paper Balanced", version: 1 } : null,
    market,
    message: running
      ? "Waiting for a fresh SPY breakout."
      : "Stopped. No paper trades.",
    attempts: [],
  });
  await page.route("**/api/paper/status", (r) => r.fulfill({ json: state() }));
  await page.route("**/api/alpaca/status", (r) =>
    r.fulfill({
      json: { configured: true, message: "Paper credentials configured." },
    }),
  );
  const orders = [
    {
      id: "broker-confirmed-buy",
      symbol: "SPY",
      side: "buy",
      status: "filled",
      filled_quantity: "3",
      average_fill_price: "100.3",
      filled_at: source.candles[30].start,
    },
  ];
  await page.route("**/api/alpaca/observe", (r) =>
    r.fulfill({
      json: {
        account: { equity: "100000", cash: "99699.1" },
        positions: [],
        orders,
        observed_at: new Date().toISOString(),
      },
    }),
  );
  await page.goto(base);
  await page
    .getByRole("heading", { name: "No paper-session candles yet" })
    .waitFor();
  await page.waitForFunction(() =>
    document.querySelector(".account h1")?.textContent.includes("100,000.00"),
  );
  assert.match(await page.locator(".account h1").textContent(), /100,000.00/);
  assert.equal(await page.getByLabel("Selected run").count(), 0);
  assert.equal(await page.locator("canvas").count(), 0);
  console.log(
    "PASS default paper view has broker equity and no historical chart",
  );
  await page
    .getByRole("button", { name: "Data & Alpaca ↗", exact: true })
    .click();
  await page.getByRole("button", { name: "Simulation", exact: true }).click();
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  assert.equal(await page.getByLabel("Selected run").count(), 0);
  assert.equal(
    await page.getByRole("option", { name: /SYNTHETIC/ }).count(),
    0,
  );
  console.log("PASS saved synthetic examples are absent from normal workspace");
  // Introduce a real-run listing over acceptance candles ONLY in this test.
  const listing = {
    ...rows.find((r) => r.complete),
    synthetic: false,
    label: "Browser-only imported fixture",
  };
  await page.route("**/api/runs", (r) =>
    r.fulfill({ json: [...rows, listing] }),
  );
  await page.reload();
  await page
    .getByRole("button", { name: "Data & Alpaca ↗", exact: true })
    .click();
  await page.getByRole("button", { name: "Simulation", exact: true }).click();
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await page.getByLabel("Selected run").waitFor();
  assert.equal(
    await page.getByLabel("Selected run").locator("option").count(),
    1,
  );
  await page.waitForFunction(() =>
    window.chartText.some((t) => t === "SIM BUY"),
  );
  await page.evaluate(() => (window.chartText = []));
  await page
    .getByRole("button", { name: "Data & Alpaca ↗", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Paper account", exact: true })
    .click();
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await page
    .getByRole("heading", { name: "No paper-session candles yet" })
    .waitFor();
  assert.equal(await page.locator("canvas").count(), 0);
  console.log(
    "PASS switching from backtest to paper clears historical candles/markers",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByLabel("Paper preset", { exact: true })
    .selectOption("paper-balanced");
  await page.route("**/api/paper/start", async (r) => {
    posts.push(r.request().postDataJSON());
    running = true;
    market = {
      ...source,
      session: source.evidence.coverage[0].session,
      feed: "iex",
      as_of: new Date().toISOString(),
    };
    await r.fulfill({ json: state() });
  });
  await page.route("**/api/paper/stop", async (r) => {
    running = false;
    await r.fulfill({ json: state() });
  });
  await page
    .getByRole("button", { name: "Start paper trading", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Stop new entries", exact: true })
    .waitFor();
  assert.deepEqual(posts, [
    { preset_id: "paper-balanced", version: 1, feed: "iex" },
  ]);
  await page.evaluate(() => (window.chartText = []));
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator("canvas").waitFor();
  await page.getByRole("button", { name: "Full session", exact: true }).click();
  await page.waitForFunction(() =>
    window.chartText.some((t) => t === "PAPER BUY"),
  );
  const text = await page.evaluate(() => window.chartText);
  assert(
    !text.some((t) =>
      /SIM BUY|SIM EXIT|^Entry |^Stop |^Target |^OR high|^OR low/.test(t),
    ),
    JSON.stringify(text),
  );
  assert.match(
    await page.locator("[data-testid=paper-context]").textContent(),
    /IEX minute candles/,
  );
  console.log(
    "PASS active paper chart uses broker fill only, even with poisoned backtest decisions",
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByRole("button", { name: "Stop new entries", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Start paper trading", exact: true })
    .waitFor();
  assert.equal(running, false);
  console.log("PASS Start freezes preset identity and Stop blocks new entries");
  const session = source.evidence.coverage[0].session;
  const ownedOrders = [
    orders[0],
    {
      ...orders[0],
      id: "owned-exit",
      side: "sell",
      average_fill_price: "102",
      filled_at: source.candles[34].start,
    },
  ];
  evidence = {
    attempts: [
      {
        session,
        state: "closed_price_evidence",
        preset_name: "Paper Balanced",
        preset_version: 1,
        entry_filled: "3",
        exit_filled: "3",
        gross_price_pnl_usd: "5.1",
        net_pnl_usd: null,
        observed_at: new Date().toISOString(),
        orders: ownedOrders.map((o) => ({
          ...o,
          role: o.side === "buy" ? "entry" : "bracket_exit",
        })),
      },
    ],
    orders: ownedOrders,
    holds: [],
    entries_blocked: false,
  };
  await page.reload();
  await page.getByRole("button", { name: /^Trades/ }).click();
  const journal = page.getByRole("region", {
    name: "Saved ORB paper evidence",
  });
  await journal
    .getByText("Closed gross estimate: $5.10", { exact: true })
    .waitFor();
  assert.match(await journal.textContent(), /Net P&L: Unavailable/);
  assert.match(await journal.textContent(), /Paper Balanced v1/);
  console.log(
    "PASS saved owned orders show gross-only evidence and exact preset after reload",
  );
  const exportCalls = [];
  await page.route("**/api/paper/evidence?*", async (route) => {
    const u = new URL(route.request().url()),
      after = u.searchParams.get("after");
    exportCalls.push(Object.fromEntries(u.searchParams));
    await route.fulfill({
      json: {
        observations: [{ seq: after === "0" ? 1 : 2 }],
        next_after: after === "0" ? 1 : 2,
        through: 2,
        has_more: after === "0",
      },
    });
  });
  const downloaded = page.waitForEvent("download");
  await journal
    .getByRole("button", { name: "Export saved paper evidence", exact: true })
    .click();
  const file = await downloaded;
  const exported = JSON.parse(await readFile(await file.path(), "utf8"));
  assert.deepEqual(
    exported.observations.map((o) => o.seq),
    [1, 2],
  );
  assert.deepEqual(exportCalls, [{ after: "0" }, { after: "1", through: "2" }]);
  assert.equal(exported.report.attempts[0].net_pnl_usd, null);
  console.log(
    "PASS paper evidence export follows a frozen cursor and retains unavailable net P&L",
  );
  const refreshCalls = [];
  await page.route("**/api/paper/evidence/refresh", async (route) => {
    refreshCalls.push(route.request().postDataJSON());
    evidence = {
      ...evidence,
      entries_blocked: true,
      holds: [{ day: session, reason: "cumulative_quantity_decreased" }],
      attempts: evidence.attempts.map((a) => ({
        ...a,
        state: "correction_hold",
        gross_price_pnl_usd: null,
        hold_reason: "cumulative_quantity_decreased",
      })),
    };
    await route.fulfill({ json: evidence });
  });
  await journal
    .getByRole("button", { name: `Refresh evidence ${session}`, exact: true })
    .click();
  await journal
    .getByText("Closed gross estimate: Unavailable", { exact: true })
    .waitFor();
  assert.match(
    await journal.textContent(),
    /automatic broker actions are blocked/,
  );
  assert.deepEqual(refreshCalls, [{ session }]);
  assert.equal(posts.length, 1);
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  assert.equal(
    await page
      .getByRole("button", { name: "Start paper trading", exact: true })
      .isDisabled(),
    true,
  );
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Review required", { exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("button", { name: "Start paper trading", exact: true })
      .isDisabled(),
    true,
  );
  console.log(
    "PASS read-only evidence refresh exposes persistent correction hold without resubmission",
  );
  for (const width of [390, 768, 1500]) {
    await page.setViewportSize({ width, height: 1000 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
  }
  assert.deepEqual(errors, []);
  console.log("PASS responsive paper controls without JavaScript errors");
} finally {
  if (browser) await browser.close();
  server.kill();
  if (server.exitCode === null)
    await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
