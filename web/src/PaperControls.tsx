import React, { useEffect, useState } from "react";
import PaperAdmission from "./PaperAdmission";
import PaperManagement from "./PaperManagement";
type Row = Record<string, any>;
export default function PaperControls({
  status,
  changed,
}: {
  status: Row | null;
  changed: (s: Row) => void;
}) {
  const [presets, setPresets] = useState<Row[]>([]),
    [id, setId] = useState("paper-balanced"),
    [feed, setFeed] = useState("iex"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    fetch("/api/presets?context=paper")
      .then(async (r) => {
        if (!r.ok) throw new Error("Preset service unavailable.");
        return r.json();
      })
      .then((v) => setPresets(v.presets))
      .catch(() => setError("Could not load presets."));
  }, []);
  const selected = presets.find((p) => p.id === id),
    s = selected?.settings;
  async function action(start: boolean) {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/paper/" + (start ? "start" : "stop"), {
        method: "POST",
        ...(start
          ? {
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                preset_id: id,
                version: selected?.version,
                feed,
              }),
            }
          : {}),
      });
      const v = await r.json();
      if (!r.ok) throw new Error(v.detail || "Paper action failed.");
      changed(v);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Paper action failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="glass panel paper-controls">
      <div className="section-head">
        <h2>Paper trading</h2>
        <span className="pill">
          {status?.evidence?.entries_blocked
            ? "Review required"
            : status?.enabled
              ? "Running"
              : "Stopped"}
        </span>
      </div>
      <p className="drawer-note">{status?.message || "Checking worker…"}</p>
      <div className="selectors">
        <label>
          Paper preset
          <select
            aria-label="Paper preset"
            value={id}
            disabled={status?.enabled}
            onChange={(e) => setId(e.target.value)}
          >
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} · v{p.version}
              </option>
            ))}
          </select>
        </label>
        <label>
          Real-time feed
          <select
            aria-label="Paper feed"
            value={feed}
            disabled={status?.enabled}
            onChange={(e) => setFeed(e.target.value)}
          >
            <option value="iex">IEX</option>
            <option value="sip">SIP (requires access)</option>
          </select>
        </label>
      </div>
      {s && (
        <p className="drawer-note">
          Up to ${Number(s.initial_cash).toLocaleString()} allocated ·{" "}
          {(Number(s.risk_fraction) * 100).toFixed(2)}% risk ceiling ·{" "}
          {(Number(s.max_position_fraction) * 100).toFixed(0)}% position cap ·{" "}
          {s.target_r || "2"}R target
        </p>
      )}
      <p className="caption">
        SPY long · one attempt/session · 09:30–09:45 NY range · entries before
        11:00 NY. Actual sizing may use less risk. Starter presets are
        unvalidated experiments.
      </p>
      <div className="mode-switch">
        <button
          className="primary"
          disabled={
            busy ||
            status?.enabled ||
            status?.evidence?.entries_blocked ||
            !selected ||
            !status?.available
          }
          onClick={() => action(true)}
        >
          {busy ? "Working…" : "Start paper trading"}
        </button>
        <button
          disabled={busy || !status?.enabled}
          onClick={() => action(false)}
        >
          Stop new entries
        </button>
      </div>
      {status?.preset && (
        <p className="caption">
          Active snapshot: {status.preset.name} v{status.preset.version}. Edits
          apply only after stopping and starting with a saved version.
        </p>
      )}
      <PaperAdmission admission={status?.admission} />
      <PaperManagement management={status?.management} />
      <details>
        <summary>Operation & limits</summary>
        <p className="drawer-note">
          Keep this server and Mac awake. Closing the browser is fine. Server
          restart requires Start again to resume management; broker-held GTC
          exits remain. Stop blocks new entries and continues managing existing
          ORB orders while the server runs. Cancellation/flatten is attempted
          five minutes before close. External positions/orders block new
          entries. This first worker supports one SPY position only; research
          entry/concurrency controls above one are rejected. IEX and SIP can
          yield different signals. Historical fee/slippage fields reserve sizing
          headroom; broker fills determine execution.
        </p>
        <pre>{JSON.stringify(status?.attempts || [], null, 2)}</pre>
      </details>
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
    </section>
  );
}
