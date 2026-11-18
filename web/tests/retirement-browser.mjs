/** Real API/SQLite retirement from invented evidence; no provider or orders. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url));
const temp = await mkdtemp(tmpdir() + "/orb-retirement-"),
  base = "http://127.0.0.1:8892";
const server = spawn(
  process.env.ORB_PYTHON || "python3",
  ["web/tests/retirement_server.py", temp],
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
      if ((await fetch(base + "/api/paper/status")).ok) {
        ready = true;
        break;
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  assert(ready, logs);
  const original = await (await fetch(base + "/api/paper/status")).json();
  assert.equal(original.admission.active_count, 1);
  const session = original.attempts[0].day;
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
  const errors = [],
    actions = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.method() !== "GET" && r.url().includes("/api/paper/"))
      actions.push(r.url());
  });
  await page.route("**/api/alpaca/observe", (r) =>
    r.fulfill({
      json: {
        account: { equity: "100000", cash: "100000" },
        positions: [],
        orders: [],
        observed_at: new Date().toISOString(),
      },
    }),
  );
  await page.goto(base);
  await page.getByRole("button", { name: /^Trades/ }).click();
  const journal = page.getByRole("region", {
    name: "Saved ORB paper evidence",
  });
  await journal.getByText("Correction review", { exact: true }).click();
  await journal
    .getByRole("button", { name: `Prepare review ${session}`, exact: true })
    .click();
  const button = journal.getByRole("button", {
    name: "Retire reviewed terminal intent",
    exact: true,
  });
  await button.waitFor();
  assert(await button.isDisabled());
  await journal
    .getByLabel(
      "I accept only this gross reconstruction; trading stays blocked.",
      { exact: true },
    )
    .check();
  assert(await button.isDisabled());
  const acknowledgement = journal.getByLabel(
    "I accept this terminal reconstruction and local headroom release; holds remain and trading stays blocked.",
    { exact: true },
  );
  await acknowledgement.check();
  assert(await button.isEnabled());
  console.log(
    "PASS real prepared case requires separate explicit retirement acknowledgement",
  );
  await page.route(
    "**/api/paper/reviews/record",
    async (r) => {
      await r.fulfill({
        status: 409,
        json: {
          detail: "Review evidence changed or expired; prepare a new case.",
        },
      });
    },
    { times: 1 },
  );
  await button.click();
  await journal
    .getByText("Review evidence changed or expired; prepare a new case.", {
      exact: true,
    })
    .waitFor();
  assert(await button.isDisabled());
  assert.equal(
    (await (await fetch(base + "/api/paper/status")).json()).admission
      .active_count,
    1,
  );
  console.log(
    "PASS refused retirement leaves reservation intact and resets acknowledgement",
  );
  await acknowledgement.check();
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/api/paper/reviews/record") && r.status() === 200,
  );
  await button.click();
  const evidence = await (await response).json();
  await journal.getByText(/Local terminal intent retired/).waitFor();
  assert(evidence.entries_blocked);
  const current = await (await fetch(base + "/api/paper/status")).json();
  assert.equal(current.admission.active_count, 0);
  assert.equal(current.admission.session_attempt_count, 1);
  assert.equal(current.attempts[0].state, "done");
  assert.deepEqual(current.evidence.holds, original.evidence.holds);
  assert.equal(current.enabled, false);
  assert.match(
    await journal.innerText(),
    /Holds remain; trading stays blocked/,
  );
  console.log(
    "PASS actual transaction retires intent and headroom while retaining holds and session cap",
  );
  const download = page.waitForEvent("download");
  await journal
    .getByRole("button", { name: "Export saved paper evidence", exact: true })
    .click();
  const file = await download,
    exported = JSON.parse(await readFile(await file.path(), "utf8"));
  assert(exported.correction_reviews.some((r) => r.kind === "retirement"));
  assert(
    exported.admission_history.some(
      (r) => r.kind === "terminal_release" && r.review_case_id,
    ),
  );
  console.log(
    "PASS evidence download retains linked review decision and terminal risk release",
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
  assert(
    actions.every(
      (url) =>
        url.endsWith("/reviews/record") || url.endsWith("/reviews/prepare"),
    ),
  );
  assert.equal(
    (await (await fetch(base + "/api/paper/status")).json()).enabled,
    false,
  );
  console.log(
    "PASS responsive review UI has no JavaScript errors or start/cancel/flatten actions",
  );
} finally {
  if (browser) await browser.close();
  server.kill();
  if (server.exitCode === null)
    await new Promise((r) => server.once("exit", r));
  await rm(temp, { recursive: true, force: true });
}
