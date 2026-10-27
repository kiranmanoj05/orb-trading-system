import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import Presets from "./Presets";

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
function Chart({
  run,
  symbol,
  session,
  selected,
}: {
  run: Run;
  symbol: string;
  session: string;
  selected?: Row;
}) {
  const [focus, setFocus] = useState(true);
  const [levels, setLevels] = useState(true);
  const all = run.candles.filter(
    (c) => c.symbol === symbol && day(c.start) === session,
  );
  const opening = run.opening_ranges.find(
    (r) => r.symbol === symbol && r.session === session,
  );
  const selectedHere =
    selected?.symbol === symbol && day(selected.signal.observed_at) === session
      ? selected
      : undefined;
  const anchor = selectedHere
    ? Date.parse(selectedHere.signal.observed_at)
    : all.length
      ? Date.parse(all[0].start) + 15 * 60000
      : 0;
  const first = focus
    ? anchor - 17 * 60000
    : all.length
      ? Date.parse(all[0].start)
      : 0;
  const last = focus
    ? anchor + 25 * 60000
    : all.length
      ? Date.parse(all.at(-1)!.start) + 60000
      : 1;
  const candles = all.filter(
    (c) => Date.parse(c.start) >= first && Date.parse(c.start) <= last,
  );
  const t = selectedHere?.trade;
  const overlays: [string, any, string][] = [];
  if (opening)
    overlays.push(
      ["Range high", opening.high, "#91c5ff"],
      ["Range low", opening.low, "#91c5ff"],
    );
  if (t) {
    overlays.push(
      ["Stop", t.stop, "#f6aaa9"],
      ["Fill target", t.target, "#9ee7d5"],
    );
    if (t.entry_price) overlays.push(["Entry", t.entry_price, "#f5db9d"]);
  }
  const values = candles
    .flatMap((c) => [Number(c.low), Number(c.high)])
    .concat(
      levels
        ? overlays
            .filter((o) => o[1] != null)
            .map((o) => Number(o[1]))
            .filter(Number.isFinite)
        : [],
    );
  const lo = Math.min(...values),
    hi = Math.max(...values),
    padding = Math.max((hi - lo) * 0.16, 0.02);
  const x = (stamp: string) =>
    62 + ((Date.parse(stamp) - first) / (last - first)) * 790;
  const y = (price: any) =>
    260 - ((Number(price) - lo + padding) / (hi - lo + 2 * padding)) * 225;
  const labelY = new Map<string, number>();
  let previousLabel = 0;
  for (const [name, value] of [...overlays]
    .filter((o) => o[1] != null)
    .sort((a, b) => y(a[1]) - y(b[1]))) {
    const placed = Math.max(y(value) + 4, previousLabel + 16);
    labelY.set(name, placed);
    previousLabel = placed;
  }
  return (
    <section className="panel chart-panel">
      <div className="section-head">
        <div>
          <span className="eyebrow">Price action · 1 minute</span>
          <h2>
            {symbol} <span className="muted">/ {session}</span>
          </h2>
        </div>
        <div className="controls">
          <button aria-pressed={focus} onClick={() => setFocus(!focus)}>
            {focus ? "Show full session" : "Focus opening / trade"}
          </button>
          <label className="check">
            <input
              type="checkbox"
              checked={levels}
              onChange={(e) => setLevels(e.target.checked)}
            />
            Levels
          </label>
        </div>
      </div>
      {candles.length ? (
        <>
          <svg
            className="candles"
            viewBox="0 0 1000 310"
            role="img"
            aria-label={`${symbol} ${session} one-minute candlesticks, New York time. Missing minutes remain gaps.`}
          >
            {Array.from({ length: 5 }, (_, i) => {
              const p = lo + ((hi - lo) * i) / 4;
              return (
                <g key={i}>
                  <line
                    x1="60"
                    x2="855"
                    y1={y(p)}
                    y2={y(p)}
                    className="gridline"
                  />
                  <text x="10" y={y(p) + 4}>
                    {p.toFixed(2)}
                  </text>
                </g>
              );
            })}
            {opening && (
              <rect
                x={Math.max(62, x(opening.start))}
                y={y(opening.high)}
                width={Math.max(
                  0,
                  Math.min(852, x(opening.available_at)) -
                    Math.max(62, x(opening.start)),
                )}
                height={Math.max(0, y(opening.low) - y(opening.high))}
                fill="#78b7fb"
                opacity=".09"
              />
            )}
            {candles.map((c) => {
              const colour =
                Number(c.close) >= Number(c.open) ? "#9ee7d5" : "#f6aaa9";
              const width = Math.max(
                0.8,
                Math.min(9, (790 / ((last - first) / 60000)) * 0.55),
              );
              return (
                <g key={c.start}>
                  <title>
                    {ny(c.start)} NY · O {c.open} H {c.high} L {c.low} C{" "}
                    {c.close} · Volume {c.volume}
                  </title>
                  <line
                    x1={x(c.start)}
                    x2={x(c.start)}
                    y1={y(c.high)}
                    y2={y(c.low)}
                    stroke={colour}
                  />
                  <rect
                    x={x(c.start) - width / 2}
                    y={Math.min(y(c.open), y(c.close))}
                    width={width}
                    height={Math.max(1, Math.abs(y(c.open) - y(c.close)))}
                    fill={colour}
                  />
                </g>
              );
            })}
            {levels &&
              overlays.map(
                ([name, value, colour]) =>
                  value != null && (
                    <g key={name}>
                      <line
                        x1="62"
                        x2="852"
                        y1={y(value)}
                        y2={y(value)}
                        stroke={colour}
                        strokeDasharray="5 5"
                        opacity=".65"
                      />
                      <text x="864" y={labelY.get(name)} fill={colour}>
                        {name} {Number(value).toFixed(2)}
                      </text>
                    </g>
                  ),
              )}
            {selectedHere &&
              [
                ["Signal", selectedHere.signal.observed_at],
                ["Fill", t?.entry_at],
                ["Exit", t?.exit_at],
              ].map(
                ([label, stamp], i) =>
                  stamp &&
                  x(stamp) >= 62 &&
                  x(stamp) <= 852 && (
                    <g key={label}>
                      <line
                        x1={x(stamp)}
                        x2={x(stamp)}
                        y1="24"
                        y2="265"
                        stroke="#d9e8ff"
                        opacity=".25"
                      />
                      <text x={x(stamp) + 4} y={25 + i * 17}>
                        {label}
                      </text>
                    </g>
                  ),
              )}
            {[0, 0.25, 0.5, 0.75, 1].map((v) => (
              <text key={v} x={62 + v * 790} y="294" textAnchor="middle">
                {ny(new Date(first + (last - first) * v).toISOString())}
              </text>
            ))}
          </svg>
          <div className="chart-foot">
            <span>New York time · USD · historical review</span>
            <span>Up / flat: mint · Down: rose · Missing minute: gap</span>
          </div>
          <details>
            <summary>
              Accessible candle table ({candles.length} visible bars)
            </summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {["NY start", "Open", "High", "Low", "Close", "Volume"].map(
                      (v) => (
                        <th key={v}>{v}</th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {candles.map((c) => (
                    <tr key={c.start}>
                      <td>{ny(c.start)}</td>
                      {["open", "high", "low", "close", "volume"].map((k) => (
                        <td key={k}>{c[k]}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      ) : (
        <p>No candles for this symbol/session. Missing data is not repaired.</p>
      )}
      <p className="caption">
        Opening range freezes at 09:45 NY. Signal reference and actual modeled
        fill targets can differ. Intraminute event order is unknown.
      </p>
    </section>
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
  close,
  onImported,
}: {
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
          Saved evaluation JSON (optional, v4/v5)
          <input
            type="file"
            accept=".json"
            onChange={(e) => setReport(e.target.files?.[0] || null)}
          />
        </label>
        <small>
          Up to 31 calendar days / 20,000 bars / 10 MiB. Imports stay in this
          server session and clear on restart. No credentials or .env files.
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
function App() {
  const [runs, setRuns] = useState<Listing[]>([]),
    [id, setId] = useState(""),
    [run, setRun] = useState<Run | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [tab, setTab] = useState("Overview"),
    [symbol, setSymbol] = useState("SPY"),
    [session, setSession] = useState(""),
    [chosen, setChosen] = useState(""),
    [importing, setImporting] = useState(false);
  const [opaque, setOpaque] = useState(() => {
    try {
      return localStorage.getItem("orb-opaque") === "true";
    } catch {
      return false;
    }
  });
  async function refresh(preferred?: string) {
    const list: Listing[] = await api("/api/runs");
    setRuns(list);
    setId(preferred || list[0]?.id || "");
  }
  useEffect(() => {
    refresh()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    if (!id) return;
    let current = true;
    setLoading(true);
    setError("");
    setRun(null);
    api("/api/runs/" + id)
      .then((r) => {
        if (current) {
          setRun(r);
          setSession(
            r.evidence.coverage[0]?.session ||
              r.applied_settings.values.start_session,
          );
          setChosen(r.decisions[0]?.signal_id || "");
          setSymbol(r.decisions[0]?.symbol || "SPY");
        }
      })
      .catch((e) => {
        if (current) setError(e.message);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [id]);
  useEffect(() => {
    document.documentElement.dataset.opaque = String(opaque);
    try {
      localStorage.setItem("orb-opaque", String(opaque));
    } catch {}
  }, [opaque]);
  const e = run?.evidence,
    s = e?.summary,
    config = run?.applied_settings.values,
    account = e?.portfolio.account;
  const decision = run?.decisions.find((d) => d.signal_id === chosen);
  const sessions: string[] = run
    ? [...new Set<string>(e!.coverage.map((c: Row) => c.session))]
    : [];
  function exportRun() {
    if (!run) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(run, null, 2) + "\n"], {
        type: "application/json",
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `orb-dashboard-${run.evaluation_id.slice(0, 12)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="shell">
      <aside className="sidebar glass">
        <a className="brand" href="#">
          <span className="orb" />
          ORB<span className="brand-small">WORKSPACE</span>
        </a>
        <div className="workspace-label">Research desk</div>
        <nav aria-label="Workspace">
          {["Overview", "Decisions", "Applied settings", "Data & health"].map(
            (v, i) => (
              <button
                key={v}
                aria-current={tab === v ? "page" : undefined}
                className={tab === v ? "active" : ""}
                onClick={() => setTab(v)}
              >
                <span className="nav-index">0{i + 1}</span>
                {v}
              </button>
            ),
          )}
        </nav>
        <div className="sidebar-bottom">
          <Pill>Historical simulation</Pill>
          <p>
            Read-only engine evidence.
            <br />
            Order submission unavailable.
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={opaque}
              onChange={(e) => setOpaque(e.target.checked)}
            />
            Reduce transparency
          </label>
          <small>Local session · SPY / QQQ</small>
        </div>
      </aside>
      <main>
        <header className="topbar glass">
          <span className="breadcrumb">
            Workspace <span>/</span> {tab}
          </span>
          <div className="controls">
            <Pill>Offline research</Pill>
            <button onClick={() => setImporting(true)}>+ Import run</button>
          </div>
        </header>
        <div className="content">
          <div className="title-row">
            <div>
              <span className="eyebrow">Opening range breakout</span>
              <h1>
                {tab === "Overview" ? "A clearer view of every decision." : tab}
              </h1>
              <p className="muted">
                Inspect the evidence. Understand the limits.
              </p>
            </div>
            <label className="run-picker">
              Selected run
              <select
                aria-label="Selected run"
                value={id}
                onChange={(e) => setId(e.target.value)}
              >
                <option value="" disabled>
                  Choose a run
                </option>
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.synthetic ? "SYNTHETIC · " : ""}
                    {r.label}
                    {r.preset ? ` · ${r.preset.name} v${r.preset.version}` : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {error && (
            <div role="alert" className="warning">
              {error}
              <button
                onClick={() => {
                  setError("");
                  refresh(id).catch((e) => setError(e.message));
                }}
              >
                Retry connection
              </button>
            </div>
          )}
          {loading && (
            <p role="status" className="panel">
              Loading engine evidence…
            </p>
          )}
          {!loading && !run && !error && (
            <section className="panel">
              <h2>Your research starts with evidence.</h2>
              <p>
                No runs loaded. Import canonical CSV or a capture bundle, or
                restart with --examples for synthetic acceptance cases.
              </p>
              <button onClick={() => setImporting(true)}>
                Import your first run
              </button>
            </section>
          )}
          {!run && !loading && tab === "Applied settings" && (
            <Presets
              onApplied={async (newId) => {
                await refresh(newId);
              }}
            />
          )}
          {run && (
            <>
              <div
                className={`notice ${run.metrics_eligible ? "" : "warning"}`}
                role="status"
              >
                <span>
                  {run.import_context.synthetic === true
                    ? "SYNTHETIC EXAMPLE · Invented prices. "
                    : ""}
                  {run.metrics_eligible
                    ? "Complete within declared scope."
                    : "Incomplete evidence · aggregate return and drawdown withheld."}
                </span>
                <span>
                  Provider authenticity unverified · {config.start_session} →{" "}
                  {config.end_session}
                </span>
              </div>
              <p className="assumptions">
                Costs: {config.slippage_bps} bps per market side ·{" "}
                {config.fee_per_share} USD/share/side.
                {Number(config.slippage_bps) === 0 &&
                Number(config.fee_per_share) === 0
                  ? " Zero-cost diagnostic; no execution-cost realism established."
                  : " Assumed costs; not calibrated provider execution."}{" "}
                Idealised immediate full fills.
              </p>
              {(tab === "Overview" || tab === "Decisions") && (
                <>
                  <div className="stats">
                    <Stat
                      label="Account return"
                      value={pct(s.total_return)}
                      note={
                        run.metrics_eligible
                          ? "Declared historical scope"
                          : "Withheld: incomplete evidence"
                      }
                    />
                    <Stat
                      label="Closed subset P&L"
                      value={money(s.closed_subset.net_pnl)}
                      note={`${s.closed_subset.count} closed · ${s.candidate_count} candidates`}
                    />
                    <Stat
                      label="Maximum drawdown"
                      value={pct(s.max_drawdown)}
                      note="Completed-minute samples only"
                    />
                    <Stat
                      label="Cash at end"
                      value={money(account.cash)}
                      note="Historical account · not Alpaca cash"
                    />
                  </div>
                  <div className="view-controls">
                    <label>
                      Symbol
                      <select
                        value={symbol}
                        onChange={(e) => {
                          setSymbol(e.target.value);
                          const d = run.decisions.find(
                            (d) =>
                              d.symbol === e.target.value &&
                              day(d.signal.observed_at) === session,
                          );
                          setChosen(d?.signal_id || "");
                        }}
                      >
                        <option>SPY</option>
                        <option>QQQ</option>
                      </select>
                    </label>
                    <label>
                      Session (New York)
                      <select
                        value={session}
                        onChange={(e) => {
                          setSession(e.target.value);
                          const d = run.decisions.find(
                            (d) =>
                              d.symbol === symbol &&
                              day(d.signal.observed_at) === e.target.value,
                          );
                          setChosen(d?.signal_id || "");
                        }}
                      >
                        {sessions.map((d) => (
                          <option key={d}>{d}</option>
                        ))}
                      </select>
                    </label>
                    <span className="muted">
                      Retrospective candles · no live feed
                    </span>
                  </div>
                  <Chart
                    run={run}
                    symbol={symbol}
                    session={session}
                    selected={decision}
                  />
                  <div className="two-col">
                    <section className="panel">
                      <div className="section-head">
                        <div>
                          <span className="eyebrow">
                            Recorded engine outcomes
                          </span>
                          <h2>Decision journal</h2>
                        </div>
                        <Pill>{String(run.decisions.length)} candidates</Pill>
                      </div>
                      <div className="decision-list">
                        {run.decisions.map((d) => (
                          <button
                            className={`decision ${chosen === d.signal_id ? "selected" : ""}`}
                            key={d.signal_id}
                            onClick={() => {
                              setChosen(d.signal_id);
                              setSymbol(d.symbol);
                              setSession(day(d.signal.observed_at));
                            }}
                          >
                            <span>
                              <strong>{d.symbol}</strong>
                              <small>
                                {day(d.signal.observed_at)} ·{" "}
                                {ny(d.signal.observed_at)} NY
                              </small>
                            </span>
                            <span>
                              <b>{words(d.outcome_class)}</b>
                              <small>{words(d.reason)}</small>
                            </span>
                          </button>
                        ))}
                      </div>
                      {e!.portfolio.replay.sessions
                        .filter((o: Row) => o.state !== "signalled")
                        .map((o: Row) => (
                          <div
                            className="session-row"
                            key={o.session + o.symbol}
                          >
                            <strong>
                              {o.symbol} · {o.session}
                            </strong>
                            <span>
                              {o.reason
                                ? words(o.reason)
                                : o.state === "expired"
                                  ? "No qualifying breakout before cutoff"
                                  : words(o.state)}
                            </span>
                          </div>
                        ))}
                    </section>
                    <section className="panel">
                      <span className="eyebrow">Decision evidence</span>
                      <h2>
                        {decision
                          ? `${decision.symbol} · ${words(decision.reason)}`
                          : "No candidate selected"}
                      </h2>
                      {decision ? (
                        <>
                          <Pair
                            name="Outcome"
                            value={words(decision.outcome_class)}
                          />
                          <Pair
                            name="Completed close reference"
                            value={money(decision.signal.entry_reference)}
                          />
                          <Pair
                            name="Modeled entry / exit"
                            value={`${money(decision.trade.entry_price)} / ${money(decision.trade.exit_price)}`}
                          />
                          <Pair
                            name="Stop / filled target"
                            value={`${money(decision.trade.stop)} / ${money(decision.trade.target)}`}
                          />
                          <Pair
                            name="Quantity / fees"
                            value={`${decision.trade.quantity} shares / ${money(decision.trade.fees)}`}
                          />
                          {decision.events
                            .filter((v: Row) => v.sizing)
                            .map((v: Row) => (
                              <div className="inset" key={v.at}>
                                <strong>{words(v.sizing.reason)}</strong>
                                <Pair
                                  name="Risk budget / deployed"
                                  value={`${money(v.sizing.risk_budget)} / ${money(v.sizing.planned_risk)}`}
                                />
                                <p>
                                  Position, cash and shared risk caps can reduce
                                  deployed risk. A planned limit does not
                                  guarantee the final loss.
                                </p>
                              </div>
                            ))}
                          <details>
                            <summary>Exact events & availability times</summary>
                            <pre>{JSON.stringify(decision, null, 2)}</pre>
                          </details>
                        </>
                      ) : (
                        <p>
                          Choose a candidate to inspect its recorded rules,
                          sizing and account events.
                        </p>
                      )}
                    </section>
                  </div>
                  {tab === "Overview" && (
                    <div className="two-col">
                      <section className="panel">
                        <span className="eyebrow">Marked account history</span>
                        <h2>Equity, with gaps intact</h2>
                        <Equity samples={e!.valuation.samples} />
                      </section>
                      <section className="panel">
                        <span className="eyebrow">End-of-run accounting</span>
                        <h2>Capital & commitments</h2>
                        <Pair
                          name="Available / reserved cash"
                          value={`${money(account.available_cash)} / ${money(account.reserved_cash)}`}
                        />
                        <Pair
                          name="Entry-cost gross exposure"
                          value={money(account.gross_exposure)}
                        />
                        <Pair
                          name="Retained initial stop risk"
                          value={money(account.committed_stop_risk)}
                        />
                        <Pair
                          name="Active allocations"
                          value={
                            account.allocations.filter(
                              (a: Row) => a.state !== "closed",
                            ).length
                          }
                        />
                        <details>
                          <summary>All allocation records</summary>
                          <pre>
                            {JSON.stringify(account.allocations, null, 2)}
                          </pre>
                        </details>
                        <p className="caption">
                          Carrying values are distinct from market marks.
                          Unresolved positions keep capacity allocated.
                        </p>
                      </section>
                    </div>
                  )}
                </>
              )}
              {tab === "Applied settings" && (
                <>
                  <Presets
                    runId={id}
                    applied={run.applied_settings.preset}
                    onApplied={async (newId) => {
                      await refresh(newId);
                    }}
                  />
                  <section className="panel">
                    <div className="section-head">
                      <div>
                        <span className="eyebrow">
                          Immutable applied configuration
                        </span>
                        <h2>
                          {run.applied_settings.preset
                            ? `${run.applied_settings.preset.name} · v${run.applied_settings.preset.version}`
                            : "ORB Reference signal rules"}
                        </h2>
                      </div>
                      <Pill>Read only</Pill>
                    </div>
                    <p>
                      These are the immutable inputs used by this run. The
                      preset editor above creates saved versions and separate
                      evaluations; it cannot rewrite these results.
                    </p>
                    <div className="two-col">
                      <div>
                        {[
                          ["Starting cash", money(config.initial_cash)],
                          ["Risk per trade", pct(config.risk_fraction)],
                          [
                            "Position / gross allocation cap",
                            `${pct(config.max_position_fraction)} / ${pct(config.max_gross_fraction)}`,
                          ],
                          [
                            "Aggregate initial stop risk",
                            pct(config.max_total_risk_fraction),
                          ],
                          [
                            "Daily loss entry-block threshold",
                            pct(config.daily_loss_fraction),
                          ],
                          [
                            "Market slippage per side",
                            `${config.slippage_bps} bps`,
                          ],
                          [
                            "Fee per share per side",
                            `${config.fee_per_share} USD`,
                          ],
                        ].map(([name, value]) => (
                          <Pair key={name} name={name} value={value} />
                        ))}
                      </div>
                      <div>
                        {[
                          ["Symbols / direction", "SPY, QQQ / long"],
                          ["Opening range", "09:30–09:45 New York"],
                          [
                            "Entry eligibility",
                            "Completed close > frozen high, before 11:00",
                          ],
                          ["Stop / planned target", "Range midpoint / 2R"],
                          ["Candidate cap", "1 per symbol / session"],
                          ["Schedule", "All eligible exchange weekdays"],
                          [
                            "Session entry / concurrency caps",
                            "Not implemented as configurable controls",
                          ],
                        ].map(([name, value]) => (
                          <Pair key={name} name={name} value={value} />
                        ))}
                      </div>
                    </div>
                    <p className="caption">
                      Daily loss blocks new entries; it is not an automatic
                      liquidation instruction. Cost assumptions are scenarios,
                      not measured provider execution costs.
                    </p>
                    <details>
                      <summary>
                        Configuration fingerprint & exact values
                      </summary>
                      <code>{run.applied_settings.sha256}</code>
                      <pre>{JSON.stringify(config, null, 2)}</pre>
                    </details>
                  </section>
                </>
              )}
              {tab === "Data & health" && (
                <>
                  <div className="two-col">
                    <section className="panel">
                      <span className="eyebrow">
                        Independent completeness gates
                      </span>
                      <h2>Can we interpret this run?</h2>
                      {Object.entries(run.health).map(([key, value]) => (
                        <Pair
                          key={key}
                          name={words(key)}
                          value={value ? "Complete" : "Incomplete"}
                        />
                      ))}
                      <Pair
                        name="Aggregate metrics"
                        value={run.metrics_eligible ? "Eligible" : "Withheld"}
                      />
                      <Pair
                        name="Broker reconciliation"
                        value="Not applicable · historical simulation"
                      />
                      <Pair
                        name="Last historical candle available"
                        value={run.candles.at(-1)?.available_at}
                      />
                      <p className="caption">
                        No live freshness or paper account status is claimed.
                        Imported timestamps describe historical evidence.
                      </p>
                    </section>
                    <section className="panel">
                      <span className="eyebrow">Daily-loss gates</span>
                      <h2>Recorded session state</h2>
                      {e!.portfolio.daily_loss_sessions.map((v: Row) => (
                        <div className="inset" key={v.session}>
                          <strong>{v.session}</strong>
                          <Pair
                            name="New entries"
                            value={
                              v.blocked
                                ? `Blocked: ${words(v.reason || "unknown")}`
                                : "Not blocked by daily-loss gate"
                            }
                          />
                          <Pair
                            name="Opening equity / loss budget"
                            value={`${money(v.reference_equity)} / ${money(v.loss_limit)}`}
                          />
                        </div>
                      ))}
                    </section>
                  </div>
                  <section className="panel">
                    <h2>Coverage by symbol and session</h2>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            {[
                              "Session",
                              "Symbol",
                              "State",
                              "Observed / expected",
                              "First missing (UTC)",
                            ].map((v) => (
                              <th key={v}>{v}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {e!.coverage.map((c: Row) => (
                            <tr key={c.symbol + c.session}>
                              <td>{c.session}</td>
                              <td>{c.symbol}</td>
                              <td>{c.state}</td>
                              <td>
                                {c.observed_minutes} / {c.expected_minutes}
                              </td>
                              <td>{c.first_missing_at || "None"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                  <section className="panel">
                    <h2>Provenance & limitations</h2>
                    <p>{run.import_context.notice}</p>
                    <Pair
                      name="Imported report identity"
                      value={
                        run.import_context.original_evaluation_id ||
                        "No saved report paired"
                      }
                    />
                    <Pair
                      name="Dataset SHA-256"
                      value={e!.manifest.dataset.sha256}
                    />
                    <Pair
                      name="Current engine SHA-256"
                      value={e!.manifest.engine.source_sha256}
                    />
                    {e!.manifest.dataset.source_bundle && (
                      <Pair
                        name="Source bundle"
                        value={`${e!.manifest.dataset.source_bundle.bundle_id} · ${e!.manifest.dataset.source_bundle.request.feed}`}
                      />
                    )}
                    <ul>
                      {[...run.limitations, ...s.limitations].map(
                        (v: string, i: number) => (
                          <li key={i}>{v}</li>
                        ),
                      )}
                    </ul>
                  </section>
                </>
              )}
              <footer>
                <span>
                  Run {run.evaluation_id.slice(0, 16)} · Settings{" "}
                  {run.applied_settings.sha256.slice(0, 12)}
                </span>
                <button onClick={exportRun}>Export dashboard evidence</button>
              </footer>
            </>
          )}
          <div className="design-credit">
            Glass material adapted for the web from{" "}
            <a
              href="https://github.com/affaan-m/ECC/tree/main/skills/liquid-glass-design"
              target="_blank"
              rel="noreferrer"
            >
              ECC design principles
            </a>
            . Original CSS · no external fonts, imagery or runtime CDN.
          </div>
        </div>
      </main>
      {importing && (
        <ImportDialog
          close={() => setImporting(false)}
          onImported={async (id) => {
            await refresh(id);
            setTab("Overview");
          }}
        />
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
