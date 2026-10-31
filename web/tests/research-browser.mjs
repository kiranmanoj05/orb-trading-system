import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const root = fileURLToPath(new URL("../..", import.meta.url));
const temp = await mkdtemp(join(tmpdir(), "orb-research-"));
const base = "http://127.0.0.1:8881";
let server,
  browser,
  logs = "";
async function start(examples = false, fail = false) {
  const code = `from orb.dashboard_api import create_app\nimport orb.research_store as m\nimport uvicorn\n${fail ? "def fail(*args): raise ValueError('injected private failure')\nm.apply_preset=fail\n" : ""}app=create_app(port=8881,examples=${examples ? "True" : "False"},web_dir=${JSON.stringify(root + "/web/build")},preset_db=${JSON.stringify(temp + "/p.sqlite")},research_db=${JSON.stringify(temp + "/r.sqlite")})\nuvicorn.run(app,host='127.0.0.1',port=8881,access_log=False)`;
  server = spawn(process.env.ORB_PYTHON || "python3", ["-c", code], {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stderr.on("data", (b) => (logs += b));
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw Error(logs);
    try {
      if ((await fetch(base + "/api/runs")).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw Error(logs);
}
async function stop() {
  const ended = new Promise((r) => server.once("exit", r));
  server.kill();
  await ended;
  server = null;
}
const json = async (path) => (await fetch(base + path)).json();
try {
  await start(true);
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
  const listing = await json("/api/runs"),
    source = listing.find((r) => !r.complete).id;
  const before = await json("/api/runs/" + source),
    sourcePackage = await json("/api/runs/" + source + "/source");
  await page.getByLabel("Selected run", { exact: true }).selectOption(source);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Applied settings/ })
    .click();
  await page
    .getByRole("button", { name: "Duplicate saved preset", exact: true })
    .click();
  await page.waitForFunction(
    () => !document.querySelector("fieldset").disabled,
  );
  await page
    .getByLabel("Preset name", { exact: true })
    .fill("Queued cost study");
  const fee = page.getByLabel("Fee per share per side (USD)", { exact: true });
  await fee.fill("0.01");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved Queued cost study v2. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByRole("button", { name: "Queue saved version", exact: true })
    .click();
  await page.getByText(/Queued Queued cost study v2/).waitFor();
  const first = (await json("/api/jobs")).find((j) => j.state === "queued");
  assert.equal(first.request.preset.version, 2);
  await fee.fill("0.02");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await page
    .getByText("Saved Queued cost study v3. Existing runs unchanged.", {
      exact: true,
    })
    .waitFor();
  assert.deepEqual(await json("/api/runs/" + source), before);
  console.log(
    "PASS queue freezes exact saved version and source while later edits remain independent",
  );
  await stop();
  await start();
  await page.reload();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Research jobs/ })
    .click();
  await page
    .getByRole("heading", {
      name: "Keep the evidence. Freeze the experiment.",
      exact: true,
    })
    .waitFor();
  assert.equal(
    (await json("/api/jobs")).find((j) => j.id === first.id).state,
    "queued",
  );
  assert.deepEqual(
    await json("/api/runs/" + source + "/source"),
    sourcePackage,
  );
  await page
    .getByRole("button", { name: "Run next queued job", exact: true })
    .click();
  await page.getByText("Job succeeded.", { exact: true }).waitFor();
  const done = (await json("/api/jobs")).find((j) => j.id === first.id);
  assert(done.result_id);
  const result = await json("/api/runs/" + done.result_id);
  assert.equal(result.applied_settings.preset.version, 2);
  assert.equal(result.evidence.manifest.config.fee_per_share, "0.01");
  assert.equal(result.metrics_eligible, false);
  assert.equal(result.evidence.summary.total_return, null);
  await page
    .getByRole("button", {
      name: "Open result " + done.result_id.slice(0, 8),
      exact: true,
    })
    .click();
  await page.getByText("Cash at end", { exact: true }).waitFor();
  assert.equal(
    await page.getByLabel("Selected run", { exact: true }).inputValue(),
    done.result_id,
  );
  console.log(
    "PASS actual server restart retains queued work and completed incomplete-evidence result with working navigation",
  );
  await stop();
  await start(false, true);
  await page.reload();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Applied settings/ })
    .click();
  await page
    .getByLabel("Saved preset", { exact: true })
    .selectOption(first.request.preset.id);
  await page
    .getByRole("button", { name: "Queue saved version", exact: true })
    .click();
  await page.getByText(/Queued Queued cost study v3/).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Research jobs/ })
    .click();
  await page
    .getByRole("button", { name: "Run next queued job", exact: true })
    .click();
  await page.getByText("Job failed.", { exact: true }).waitFor();
  const failed = (await json("/api/jobs")).find((j) => j.state === "failed");
  assert(failed);
  assert.equal(failed.error, "evaluation_failed");
  assert(!JSON.stringify(failed).includes("private"));
  await page
    .getByRole("button", {
      name: "Queue retry " + failed.id.slice(0, 8),
      exact: true,
    })
    .click();
  await page.getByText("Job queued.", { exact: true }).waitFor();
  console.log(
    "PASS failed evaluations retain honest sanitised status and explicit retry creates queued history",
  );
  await stop();
  await start();
  await page.reload();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: /Research jobs/ })
    .click();
  await page
    .getByRole("button", { name: "Run next queued job", exact: true })
    .click();
  await page.getByText("Job succeeded.", { exact: true }).waitFor();
  const retried = (await json("/api/jobs")).find(
    (j) => j.request.retry_of === failed.id,
  );
  assert.equal(retried.state, "succeeded");
  assert.equal(
    (await json("/api/jobs")).find((j) => j.id === failed.id).state,
    "failed",
  );
  assert.deepEqual(await json("/api/runs/" + done.result_id), result);
  await page
    .getByRole("button", { name: "Run next queued job", exact: true })
    .click();
  await page.getByText("No queued jobs.", { exact: true }).waitFor();
  assert.deepEqual(errors, []);
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
  if (process.env.ORB_RESEARCH_SCREENSHOT)
    await page.screenshot({
      path: process.env.ORB_RESEARCH_SCREENSHOT,
      fullPage: true,
    });
  console.log(
    "PASS retry survives restart, old snapshots stay immutable, empty queue/reflow/reduced-motion checks pass",
  );
} finally {
  if (browser) await browser.close();
  if (server) await stop();
  await rm(temp, { recursive: true, force: true });
}
