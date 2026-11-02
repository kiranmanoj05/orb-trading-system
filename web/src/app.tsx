import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import Presets from "./Presets";
import PaperControls from "./PaperControls";
import ResearchJobs from "./ResearchJobs";
import Comparisons from "./Comparisons";
import CandleChart from "./CandleChart";
import Appearance, { Logo } from "./Appearance";

type Row = Record<string, any>; // Read-only engine evidence; never sent as execution instructions.
type Run = {
  evaluation_id: string;
  health: Record<string, boolean>;
  metrics_eligible: boolean;
  candles: Row[];
  opening_ranges: Row[];
  decisions: Row[];
  evidence: Row;
  applied_settings: Row;
  import_context: Row;
  limitations: string[];
};
type Listing = {
  preset?: { name: string; version: number };
  trial?: { name: string; version: number; period: string; cost: string };
  id: string;
  label: string;
  complete: boolean;
  synthetic: boolean | null;
  start: string;
  end: string;
};
const ny = (t: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(t));
const day = (t: string) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(t));
const money = (n: any) =>
  n == null
    ? "Unavailable"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(Number(n));
const pct = (n: any) =>
  n == null ? "Unavailable" : `${(Number(n) * 100).toFixed(2)}%`;
const words = (s: string) => s.replaceAll("_", " ");
async function api(path: string, options?: RequestInit) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      body.detail || "The local service could not complete this request.",
    );
  return body;
}
function Pill({
  children,
  good = false,
}: {
  children: React.ReactNode;
  good?: boolean;
}) {
  return <span className={`pill ${good ? "good" : ""}`}>{children}</span>;
}
function Stat({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note: string;
}) {
  return (
    <article className="stat">
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </article>
  );
}
function Pair({ name, value }: { name: string; value: React.ReactNode }) {
  return (
    <div className="pair">
      <span>{name}</span>
      <strong>{value}</strong>
    </div>
  );
}
function Equity({ samples }: { samples: Row[] }) {
  const known = samples
    .filter((s) => s.equity != null)
    .map((s) => Number(s.equity));
  if (!known.length) return <p>No valued account samples.</p>;
  const min = Math.min(...known),
    max = Math.max(...known),
    span = Math.max(1, max - min);
  const first = Date.parse(samples[0].at),
    last = Date.parse(samples.at(-1)!.at);
  const paths: string[] = [];
  let path = "";
  samples.forEach((s) => {
    if (s.equity == null) {
      if (path) paths.push(path);
      path = "";
    } else {
      const x =
          25 + ((Date.parse(s.at) - first) / Math.max(1, last - first)) * 850,
        y = 133 - ((Number(s.equity) - min) / span) * 95;
      path += `${path ? "L" : "M"}${x},${y} `;
    }
  });
  if (path) paths.push(path);
  return (
    <>
      <svg
        className="equity"
        viewBox="0 0 900 165"
        role="img"
        aria-label="Known account equity samples; missing marks break the line"
      >
        <line x1="25" x2="875" y1="140" y2="140" className="gridline" />
        {paths.map((d, i) => (
          <path key={i} d={d} fill="none" stroke="#a4d7ff" strokeWidth="2.5" />
        ))}
        <text x="25" y="20">
          {money(max)}
        </text>
        <text x="25" y="160">
          {money(min)}
        </text>
      </svg>
      <p className="caption">
        Known samples only. Missing marks break the line; this chart does not
        override metric eligibility.
      </p>
    </>
  );
}
function ImportDialog({
  durable,
  close,
  onImported,
}: {
  durable: boolean;
  close: () => void;
  onImported: (id: string) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const [kind, setKind] = useState("csv"),
    [source, setSource] = useState<File | null>(null),
    [report, setReport] = useState<File | null>(null),
    [bars, setBars] = useState<File | null>(null),
    [manifest, setManifest] = useState<File | null>(null),
    [label, setLabel] = useState("Local research import"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (!source) throw new Error("Choose a source file.");
      const files = [source, report, bars, manifest].filter(Boolean) as File[];
      if (files.reduce((s, f) => s + f.size, 0) > 10 * 1024 * 1024)
        throw new Error("Files exceed the 10 MiB import limit.");
      const payload: Row = { kind, label, source: await source.text() };
      if (report) payload.report = JSON.parse(await report.text());
      if (kind === "bundle") {
        if (!bars || !manifest)
          throw new Error("Choose bars.csv and manifest.json as well.");
        payload.bars_csv = await bars.text();
        payload.bundle_manifest = await manifest.text();
      }
      const result = await api("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await onImported(result.id);
      close();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Import failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      onCancel={(e) => {
        if (busy) e.preventDefault();
        else close();
      }}
      aria-labelledby="import-title"
      className="import-dialog"
    >
      <form onSubmit={submit}>
        <div className="section-head">
          <h2 id="import-title">Bring your evidence in</h2>
          <button
            type="button"
            onClick={close}
            disabled={busy}
            aria-label="Close import"
          >
            ×
          </button>
        </div>
        <p>
          Results are recomputed by the current Python engine. A saved report
          supplies its original settings and identity; its metrics are not
          trusted.
        </p>
        <label>
          Import format
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setSource(null);
            }}
          >
            <option value="csv">Canonical OHLCV CSV</option>
            <option value="capture">Alpaca capture.json</option>
            <option value="bundle">Full capture bundle (3 files)</option>
          </select>
        </label>
        <label>
          Run label
          <input
            value={label}
            maxLength={120}
            required
            onChange={(e) => setLabel(e.target.value)}
          />
        </label>
        <label>
          {kind === "csv" ? "Source CSV" : "capture.json"}
          <input
            key={kind}
            type="file"
            required
            accept={kind === "csv" ? ".csv" : ".json"}
            onChange={(e) => setSource(e.target.files?.[0] || null)}
          />
        </label>
        {kind === "bundle" && (
          <>
            <label>
              bars.csv
              <input
                type="file"
                required
                accept=".csv"
                onChange={(e) => setBars(e.target.files?.[0] || null)}
              />
            </label>
            <label>
              manifest.json
              <input
                type="file"
                required
                accept=".json"
                onChange={(e) => setManifest(e.target.files?.[0] || null)}
              />
            </label>
          </>
        )}
        <label>
          Saved evaluation JSON (optional, v4–v7)
          <input
            type="file"
            accept=".json"
            onChange={(e) => setReport(e.target.files?.[0] || null)}
          />
        </label>
        <small>
          Up to 31 calendar days / 20,000 bars / 10 MiB. Imports stay in
          {durable
            ? "a local database and survive server restarts"
            : "temporary memory and clear on restart"}
          . No credentials or .env files.
        </small>
        {error && (
          <p role="alert" className="warning">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? "Evaluating source…" : "Validate & import"}
        </button>
      </form>
    </dialog>
  );
}
function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`glass drawer ${["Research", "Comparisons"].includes(title) ? "research-drawer" : ""}`}
      onCancel={close}
    >
      <div className="drawer-head">
        <h2>{title}</h2>
        <button className="icon" aria-label="Close panel" onClick={close}>
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}
function App() {
  const pendingView = useRef<{ symbol: string; session: string } | null>(null);
  const [runs, setRuns] = useState<Listing[]>([]),
    [paperState, setPaperState] = useState<Row | null>(null),
    [id, setId] = useState(""),
    [run, setRun] = useState<Run | null>(null),
    [tab, setTab] = useState("Workspace"),
    [panel, setPanel] = useState(""),
    [appearance, setAppearance] = useState(false),
    [importing, setImporting] = useState(false),
    [symbol, setSymbol] = useState("SPY"),
    [session, setSession] = useState(""),
    [chosen, setChosen] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [durable, setDurable] = useState(false),
    [status, setStatus] = useState<Row | null>(null),
    [paper, setPaper] = useState<Row | null>(null),
    [mode, setMode] = useState("paper"),
    [feed, setFeed] = useState("sip"),
    [start, setStart] = useState(""),
    [end, setEnd] = useState(""),
    [local, setLocal] = useState<Row[]>([]),
    [comparisonId, setComparisonId] = useState("");
  async function refresh(preferred?: string) {
    const [rows, storage, connection, bundles] = await Promise.all([
      api("/api/runs"),
      api("/api/storage"),
      api("/api/alpaca/status"),
      api("/api/local-bundles"),
    ]);
    const visibleRows = rows.filter((r: Listing) => !r.synthetic);
    setRuns(visibleRows);
    setDurable(storage.durable);
    setStatus(connection);
    setLocal(bundles.imports);
    setId(
      (old) =>
        (visibleRows.some((r: Listing) => r.id === preferred)
          ? preferred
          : "") ||
        (visibleRows.some((r: Listing) => r.id === old) ? old : "") ||
        [...rows].reverse().find((r: Listing) => !r.synthetic && r.complete)
          ?.id ||
        [...rows].reverse().find((r: Listing) => !r.synthetic)?.id ||
        visibleRows[0]?.id ||
        "",
    );
  }
  useEffect(() => {
    let live = true;
    const poll = () =>
      api("/api/paper/status")
        .then((v) => {
          if (live) setPaperState(v);
        })
        .catch(() => {});
    poll();
    const timer = setInterval(poll, 15000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!id) {
      setRun(null);
      return;
    }
    let live = true;
    setLoading(true);
    api("/api/runs/" + id)
      .then((r) => {
        if (!live) return;
        setRun(r);
        const d = r.decisions[0],
          pending = pendingView.current;
        setSession(
          pending?.session ||
            r.evidence.coverage[0]?.session ||
            r.applied_settings.values.start_session,
        );
        setSymbol(pending?.symbol || d?.symbol || "SPY");
        setChosen(pending ? "" : d?.signal_id || "");
        pendingView.current = null;
      })
      .catch((e) => {
        if (live) setError(e.message);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [id]);
  async function work(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Operation failed");
    } finally {
      setBusy(false);
    }
  }
  async function observe() {
    await work(async () => {
      const value = await api("/api/alpaca/observe", { method: "POST" });
      setPaper(value);
      setMode("paper");
      setPanel("");
    });
  }
  async function fetchMarket() {
    await work(async () => {
      const result = await api("/api/alpaca/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_session: start,
          end_session: end || start,
          feed,
        }),
      });
      await refresh(result.id);
      if (!pendingView.current) setMode("simulation");
      setPanel("");
    });
  }
  useEffect(() => {
    if (mode !== "paper" || !status?.configured) return;
    let live = true;
    api("/api/alpaca/observe", { method: "POST" })
      .then((v) => {
        if (live) setPaper(v);
      })
      .catch((e) => {
        if (live) setError(e.message);
      });
    return () => {
      live = false;
    };
  }, [mode, status?.configured]);
  useEffect(() => {
    if (mode !== "paper" || !paper) return;
    let active = true;
    const timer = setInterval(() => {
      if (document.hidden || busy) return;
      api("/api/alpaca/observe", { method: "POST" })
        .then((v) => {
          if (active) setPaper(v);
        })
        .catch((e) => {
          if (active) setError("Paper refresh failed: " + e.message);
        });
    }, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [mode, !!paper, busy]);
  const evidence = run?.evidence,
    summary = evidence?.summary,
    config = run?.applied_settings.values,
    selected = run?.decisions.find((d) => d.signal_id === chosen),
    samples = evidence?.valuation.samples || [],
    lastSample = samples.at(-1);
  const balance =
    mode === "paper" ? paper?.account.equity : (lastSample?.equity ?? null);
  const sessions: string[] = run
    ? [...new Set<string>(evidence!.coverage.map((c: Row) => c.session))]
    : [];
  const paperFills = (paper?.orders || []).filter(
    (o: Row) => Number(o.filled_quantity) > 0,
  );
  function select(d: Row) {
    setChosen(d.signal_id);
    setSymbol(d.symbol);
    setSession(day(d.signal.observed_at));
    setPanel("");
    setTab("Workspace");
  }
  function exportRun() {
    if (!run) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(run, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `orb-${run.evaluation_id.slice(0, 12)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <>
      <Appearance open={appearance} onClose={() => setAppearance(false)} />
      <div className="app">
        <header>
          <div className="brand">
            <button
              className="brandmark"
              onClick={() => setPanel("Logo")}
              aria-label="Preview liquid ORB logo"
            >
              <Logo />
            </button>
            <a
              href="#"
              className="wordmark"
              onClick={(e) => {
                e.preventDefault();
                setTab("Workspace");
              }}
            >
              orb
            </a>
            <span className="demo">
              {run?.import_context.synthetic ? "Synthetic" : "Local"}
            </span>
          </div>
          <nav className="glass segmented" aria-label="Main navigation">
            {["Workspace", "Settings"].map((v) => (
              <button
                key={v}
                className={tab === v ? "active" : ""}
                onClick={() => setTab(v)}
              >
                {v}
              </button>
            ))}
          </nav>
          <button
            className="icon glass appearance"
            aria-label="Appearance"
            onClick={() => setAppearance(true)}
          >
            ☼
          </button>
        </header>
        {error && (
          <div role="alert" className="warning">
            {error}
            <button onClick={() => setError("")} aria-label="Dismiss error">
              ×
            </button>
          </div>
        )}
        {tab === "Settings" ? (
          <main className="settings-view">
            <PaperControls
              status={paperState}
              changed={(s) => {
                setPaperState({ ...s, available: true });
                setMode("paper");
              }}
            />
            <Presets
              runId={id || undefined}
              applied={run?.applied_settings.preset}
              appliedNote={
                run?.applied_settings.comparison_trial
                  ? `Comparison trial: ${run.applied_settings.comparison_trial.preset.name} v${run.applied_settings.comparison_trial.preset.version}; cost override ${run.applied_settings.comparison_trial.cost_scenario.name}. Effective values are in Details; saved preset unchanged.`
                  : undefined
              }
              onApplied={async (newId) => {
                await refresh(newId);
                setMode("simulation");
                setTab("Workspace");
              }}
            />
          </main>
        ) : (
          <main>
            <section className="account glass">
              <div>
                <p className="label">
                  {mode === "paper"
                    ? "Alpaca paper equity"
                    : "Simulation equity"}
                </p>
                <h1>{money(balance)}</h1>
                <p className="return">
                  {mode === "simulation"
                    ? run?.metrics_eligible
                      ? pct(summary?.total_return)
                      : run
                        ? "Metrics unavailable"
                        : "No run selected"
                    : paper
                      ? `Observed ${new Date(paper.observed_at).toLocaleTimeString("en-GB")}`
                      : "Not connected"}
                  <span>
                    {mode === "simulation" && run?.import_context.synthetic
                      ? "Invented test prices"
                      : mode === "simulation"
                        ? "Historical evaluation"
                        : paperState?.enabled
                          ? "Paper bot running"
                          : "Broker account"}
                  </span>
                </p>
              </div>
              <div className="account-right">
                <button className="status" onClick={() => setPanel("Data")}>
                  <i />
                  {mode === "paper"
                    ? "Paper account"
                    : "Historical simulation"}{" "}
                  ⌄
                </button>
                <button
                  className="text-button"
                  onClick={() => setPanel("Account")}
                >
                  View balance ↗
                </button>
              </div>
            </section>
            {mode === "paper" ? (
              <>
                <p className="caption" data-testid="paper-context">
                  Alpaca paper account · SPY ·{" "}
                  {paperState?.market?.session || "No current session loaded"} ·{" "}
                  {paperState?.market
                    ? `${paperState.market.feed.toUpperCase()} minute candles`
                    : "No historical simulation shown"}
                </p>
                {paperState?.market?.candles?.length ? (
                  <CandleChart
                    run={paperState.market}
                    symbol="SPY"
                    session={paperState.market.session}
                    paperOrders={paperFills}
                    mode="paper"
                  />
                ) : (
                  <section className="chart-card glass empty-workspace">
                    <h2>No paper-session candles yet</h2>
                    <p>
                      {paperState?.message ||
                        "Start the paper worker in Settings to load the current session."}
                    </p>
                    <button onClick={() => setTab("Settings")}>
                      Paper trading settings ↗
                    </button>
                  </section>
                )}
                {paperState?.market?.candles?.length > 0 && (
                  <p className="caption">{paperState?.message}</p>
                )}
              </>
            ) : loading ? (
              <p role="status" className="panel">
                Loading run…
              </p>
            ) : run ? (
              <>
                <div className="workspace-controls">
                  <label>
                    Run
                    <select
                      aria-label="Selected run"
                      value={id}
                      onChange={(e) => setId(e.target.value)}
                    >
                      {runs.map((r) => (
                        <option value={r.id} key={r.id}>
                          {r.synthetic ? "SYNTHETIC · " : ""}
                          {r.preset
                            ? `${r.preset.name} · v${r.preset.version} · `
                            : r.trial
                              ? `${r.trial.name} · v${r.trial.version} · ${r.trial.period} · ${r.trial.cost} · `
                              : ""}
                          {r.label} · {r.start}
                          {r.end !== r.start ? ` → ${r.end}` : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Symbol
                    <select
                      aria-label="Symbol"
                      value={symbol}
                      onChange={(e) => {
                        setSymbol(e.target.value);
                        setChosen("");
                      }}
                    >
                      <option>SPY</option>
                      <option>QQQ</option>
                    </select>
                  </label>
                  <label>
                    Session
                    <select
                      aria-label="Session"
                      value={session}
                      onChange={(e) => {
                        setSession(e.target.value);
                        setChosen("");
                      }}
                    >
                      {sessions.map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  </label>
                </div>
                {!run.metrics_eligible && (
                  <button
                    className="integrity-note"
                    onClick={() => setPanel("Evidence")}
                  >
                    Incomplete evidence · performance withheld ↗
                  </button>
                )}
                <CandleChart
                  run={run}
                  symbol={symbol}
                  session={session}
                  selected={selected}
                  mode="simulation"
                />
              </>
            ) : (
              <section className="chart-card glass empty-workspace">
                <h2>Your market data belongs here.</h2>
                <p>Fetch an Alpaca session or import a downloaded bundle.</p>
                <button className="primary" onClick={() => setPanel("Data")}>
                  Connect data ↗
                </button>
              </section>
            )}
            <div className="bottom-row">
              <button
                className="trade-button glass"
                onClick={() => setPanel("Trades")}
              >
                Trades{" "}
                <small>
                  {mode === "paper"
                    ? paper?.orders.length || 0
                    : run?.decisions.length || 0}
                </small>
                <span className="arrow">↗</span>
              </button>
              <button
                className="preset-button glass"
                onClick={() => setTab("Settings")}
              >
                <span className="mini-dot" />
                {(mode === "paper"
                  ? paperState?.preset?.name
                  : run?.applied_settings.preset?.name) || "Choose preset"}{" "}
                ⌄
              </button>
              <button
                className="glass utility-button"
                onClick={() => setPanel("Data")}
              >
                Data & Alpaca ↗
              </button>
            </div>
          </main>
        )}
        <footer className="site-footer">
          <span>
            {mode === "paper"
              ? paperState?.enabled
                ? "Paper bot running · paper orders enabled"
                : "Paper account · new entries stopped"
              : "Historical simulation · no broker orders"}
          </span>
          <button onClick={() => setPanel("Evidence")}>Details</button>
        </footer>
      </div>
      {importing && (
        <ImportDialog
          durable={durable}
          close={() => setImporting(false)}
          onImported={async (newId) => {
            await refresh(newId);
            setMode("simulation");
            setPanel("");
          }}
        />
      )}
      {panel && (
        <Modal
          title={panel === "Data" ? "Data & Alpaca" : panel}
          close={() => setPanel("")}
        >
          {error && (
            <p role="alert" className="warning">
              {error}
            </p>
          )}
          {panel === "Logo" && (
            <div className="orb-preview droplet-preview">
              <Logo large />
            </div>
          )}
          {panel === "Data" && (
            <>
              <div className="mode-switch">
                <button
                  aria-pressed={mode === "simulation"}
                  onClick={() => setMode("simulation")}
                >
                  Simulation
                </button>
                <button
                  aria-pressed={mode === "paper"}
                  onClick={() => setMode("paper")}
                >
                  Paper account
                </button>
              </div>
              <p className="drawer-note">
                {status?.message || "Checking local server…"}
              </p>
              <button
                className="primary"
                disabled={busy || !status?.configured}
                onClick={observe}
              >
                {busy ? "Working…" : "Refresh paper account"}
              </button>
              <p className="caption">
                Read-only balances, positions and latest 500 orders. Refreshes
                every 30 seconds while Paper account is visible. To run the bot,
                use Settings → Paper trading.
              </p>
              <h3 className="drawer-content-title">Fetch & simulate</h3>
              <p className="drawer-note">
                SPY / QQQ · completed historical sessions · ORB Reference. Apply
                a saved preset afterwards in Settings.
              </p>
              <label className="field">
                Start session
                <input
                  type="date"
                  value={start}
                  onChange={(e) => {
                    setStart(e.target.value);
                    if (!end) setEnd(e.target.value);
                  }}
                />
              </label>
              <label className="field">
                End session
                <input
                  type="date"
                  value={end}
                  onChange={(e) => setEnd(e.target.value)}
                />
              </label>
              <label className="field">
                Feed
                <select value={feed} onChange={(e) => setFeed(e.target.value)}>
                  <option value="sip">SIP</option>
                  <option value="iex">IEX</option>
                </select>
              </label>
              <button
                className="primary"
                disabled={busy || !start || !status?.configured}
                onClick={fetchMarket}
              >
                Fetch & simulate ↗
              </button>
              <h3 className="drawer-content-title">Already downloaded?</h3>
              <button onClick={() => setImporting(true)}>Import run</button>
              <button
                disabled={busy}
                onClick={() =>
                  work(async () => {
                    await api("/api/local-bundles/refresh", { method: "POST" });
                    await refresh();
                  })
                }
              >
                Refresh local bundles
              </button>
              {local.map((r: Row, i) => (
                <p className="drawer-note" key={i}>
                  {r.label}: {r.error || "Imported"}
                </p>
              ))}
              {!status?.configured && (
                <details>
                  <summary>Configure credentials</summary>
                  <p className="drawer-note">
                    Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in the terminal
                    environment before starting this server. Use your paper
                    keys. Never put keys in frontend files or chat.
                  </p>
                </details>
              )}
            </>
          )}
          {panel === "Trades" &&
            (mode === "paper" ? (
              <>
                <p className="drawer-note">
                  Broker orders and confirmed cumulative fills. Paper charts
                  never show simulated entries.
                </p>
                {paper?.history_may_be_truncated && (
                  <p className="warning">
                    500-order limit reached. Older orders are not shown.
                  </p>
                )}
                {!paper && (
                  <button onClick={() => setPanel("Data")}>
                    Refresh paper account first ↗
                  </button>
                )}
                {paper?.orders.length === 0 && (
                  <p>No broker orders returned.</p>
                )}
                {paper?.orders.map((o: Row) => (
                  <article className="broker-order" key={o.id}>
                    <strong>
                      {o.symbol} · {o.side}
                    </strong>
                    <span>{words(o.status)}</span>
                    <small>
                      {o.filled_quantity} filled @ {money(o.average_fill_price)}
                    </small>
                    <small>{o.filled_at || o.submitted_at}</small>
                    {mode !== "paper" &&
                      ["SPY", "QQQ"].includes(o.symbol) &&
                      o.filled_at && (
                        <button
                          onClick={() => {
                            const r = runs.find(
                              (v) =>
                                v.synthetic !== true &&
                                v.start <= day(o.filled_at) &&
                                v.end >= day(o.filled_at),
                            );
                            if (r) {
                              if (r.id !== id)
                                pendingView.current = {
                                  symbol: o.symbol,
                                  session: day(o.filled_at),
                                };
                              setId(r.id);
                              setSymbol(o.symbol);
                              setSession(day(o.filled_at));
                              setChosen("");
                              setPanel("");
                            } else {
                              pendingView.current = {
                                symbol: o.symbol,
                                session: day(o.filled_at),
                              };
                              setStart(day(o.filled_at));
                              setEnd(day(o.filled_at));
                              setPanel("Data");
                            }
                          }}
                        >
                          View / fetch matching candles ↗
                        </button>
                      )}
                  </article>
                ))}
              </>
            ) : (
              <>
                <p className="drawer-note">
                  Every engine candidate is retained, including blocked and
                  unresolved outcomes.
                </p>
                {!run?.decisions.length && <p>No candidates in this run.</p>}
                {run?.decisions.map((d) => (
                  <article key={d.signal_id} className="trade-detail">
                    <button className="trade-row" onClick={() => select(d)}>
                      <div>
                        <strong>
                          {d.symbol} · {words(d.status)}
                        </strong>
                        <small>
                          {day(d.signal.observed_at)} ·{" "}
                          {ny(d.signal.observed_at)} NY
                        </small>
                      </div>
                      <span className="positive">{money(d.trade.net_pnl)}</span>
                      <span>↗</span>
                    </button>
                    <details>
                      <summary>{words(d.reason)}</summary>
                      {[
                        ["Entry", d.trade.entry_price],
                        ["Stop", d.trade.stop],
                        ["Target", d.trade.target],
                        ["Exit", d.trade.exit_price],
                        ["Quantity", d.trade.quantity],
                        ["Fees", d.trade.fees],
                      ].map(([k, v]) => (
                        <Pair
                          key={k}
                          name={k}
                          value={k === "Quantity" ? String(v) : money(v)}
                        />
                      ))}
                      <p className="drawer-note">
                        Signal close {money(d.signal.entry_reference)} ·{" "}
                        {d.events
                          .map((v: Row) => words(v.reason || v.kind))
                          .join(" → ")}
                      </p>
                    </details>
                  </article>
                ))}
              </>
            ))}
          {panel === "Account" &&
            (mode === "paper" ? (
              <>
                {!paper ? (
                  <button onClick={() => setPanel("Data")}>
                    Connect paper account ↗
                  </button>
                ) : (
                  <>
                    <Pair name="Equity" value={money(paper.account.equity)} />
                    <Pair name="Cash" value={money(paper.account.cash)} />
                    <Pair
                      name="Buying power"
                      value={money(paper.account.buying_power)}
                    />
                    {paper.positions.map((p: Row) => (
                      <Pair
                        key={p.symbol}
                        name={`${p.symbol} · ${p.quantity}`}
                        value={money(p.market_value)}
                      />
                    ))}
                    <p className="drawer-note">
                      Observed {paper.observed_at}. This is not an atomic
                      reconciliation snapshot.
                    </p>
                  </>
                )}
              </>
            ) : (
              <>
                {run ? (
                  <>
                    <Pair name="Valued equity" value={money(balance)} />
                    <Pair
                      name="Cash"
                      value={money(evidence?.portfolio.account.cash)}
                    />
                    <Pair name="Return" value={pct(summary?.total_return)} />
                    <Pair
                      name="Max drawdown"
                      value={pct(summary?.max_drawdown)}
                    />
                    <Pair
                      name="Closed subset P&L"
                      value={money(summary?.closed_subset.net_pnl)}
                    />
                    <Equity samples={samples} />
                  </>
                ) : (
                  <p>No simulation selected.</p>
                )}
              </>
            ))}
          {panel === "Evidence" && mode === "paper" && (
            <p className="drawer-note">
              {paperState?.message} Broker observation:{" "}
              {paper?.observed_at || "not refreshed"}. Historical research is
              available in Simulation mode.
            </p>
          )}
          {panel === "Evidence" && mode !== "paper" && (
            <>
              <button onClick={() => setPanel("Research")}>
                Research jobs ↗
              </button>
              <button
                onClick={() => {
                  setComparisonId("");
                  setPanel("Comparisons");
                }}
              >
                Comparisons ↗
              </button>
              {run && (
                <>
                  {run.applied_settings.comparison_trial && (
                    <p className="notice">
                      Comparison trial:{" "}
                      {run.applied_settings.comparison_trial.preset.name} v
                      {run.applied_settings.comparison_trial.preset.version};
                      cost override{" "}
                      {run.applied_settings.comparison_trial.cost_scenario.name}
                      . Effective values below; saved preset unchanged.
                    </p>
                  )}
                  <p className="drawer-note">
                    {run.import_context.synthetic ? "Invented test data. " : ""}
                    {run.metrics_eligible
                      ? "Complete within declared scope."
                      : "Missing evidence: aggregate performance is withheld."}{" "}
                    Provider authenticity and point-in-time availability remain
                    unverified.
                  </p>
                  <Pair
                    name="Costs / side"
                    value={`${config.slippage_bps} bps + ${config.fee_per_share} USD/share`}
                  />
                  <p className="drawer-note">
                    Idealised full fills. Assumed costs are not calibrated
                    execution evidence.
                  </p>
                  <button onClick={exportRun}>Export dashboard evidence</button>
                  <details>
                    <summary>Data health & exact settings</summary>
                    <pre>
                      {JSON.stringify(
                        {
                          health: run.health,
                          settings: run.applied_settings,
                          coverage: evidence?.coverage,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                </>
              )}
            </>
          )}
          {panel === "Comparisons" && (
            <>
              <button onClick={() => setPanel("Research")}>
                Research jobs ↗
              </button>
              <Comparisons
                runId={id || undefined}
                comparisonId={comparisonId}
                onOpen={async (newId) => {
                  await refresh(newId);
                  setMode("simulation");
                  setPanel("");
                  setTab("Workspace");
                }}
              />
            </>
          )}
          {panel === "Research" && (
            <ResearchJobs
              onComparison={(newId) => {
                setComparisonId(newId);
                setPanel("Comparisons");
              }}
              onOpen={async (newId) => {
                await refresh(newId);
                setMode("simulation");
                setPanel("");
                setTab("Workspace");
              }}
            />
          )}
        </Modal>
      )}
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
