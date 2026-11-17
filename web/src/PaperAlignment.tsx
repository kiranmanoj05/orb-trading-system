import React, { useEffect, useRef, useState } from "react";
type Row = Record<string, any>;
const show = (v: any) =>
  v == null
    ? "Unavailable"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 });
export default function PaperAlignment({ runId }: { runId: string }) {
  const [presets, setPresets] = useState<Row[]>([]),
    [id, setId] = useState("paper-balanced"),
    [result, setResult] = useState<Row | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [open, setOpen] = useState(false);
  const epoch = useRef(0);
  const selected = presets.find((p) => p.id === id);
  useEffect(() => {
    if (!open) return;
    let active = true;
    fetch("/api/presets?context=paper")
      .then(async (r) => {
        if (!r.ok) throw Error("Saved presets unavailable.");
        return r.json();
      })
      .then((v) => {
        if (active) setPresets(v.presets);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [open]);
  useEffect(() => {
    epoch.current++;
    setResult(null);
    setBusy(false);
    setError("");
    return () => {
      epoch.current++;
    };
  }, [runId, id, selected?.version]);
  async function calculate() {
    if (!selected) return;
    const requestEpoch = ++epoch.current;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await fetch(`/api/runs/${runId}/paper-alignment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preset_id: selected.id,
          version: selected.version,
        }),
      });
      const report = await response.json();
      if (!response.ok) throw Error(report.detail || "Study unavailable.");
      if (epoch.current === requestEpoch) setResult(report);
    } catch (e) {
      if (epoch.current === requestEpoch)
        setError(e instanceof Error ? e.message : "Study unavailable.");
    } finally {
      if (epoch.current === requestEpoch) setBusy(false);
    }
  }
  function download() {
    if (!result) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(result, null, 2) + "\n"], {
        type: "application/json",
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "orb-spy-paper-preset-study.json";
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <details
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="paper-alignment"
    >
      <summary>SPY paper-preset study</summary>
      <p className="caption">
        Historical simulation with saved paper rules. Quote/limit execution and
        broker profit are not reproduced.
      </p>
      <p className="caption">
        Report only: the selected run and chart keep their original settings.
      </p>
      <label>
        Study preset
        <select
          aria-label="Study paper preset"
          value={id}
          onChange={(e) => setId(e.target.value)}
        >
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} · v{p.version}
            </option>
          ))}
        </select>
      </label>
      <button disabled={busy || !selected} onClick={calculate}>
        {busy ? "Evaluating…" : "Evaluate SPY study"}
      </button>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {result && (
        <>
          <p className="drawer-note">
            {result.preset.name} v{result.preset.version} · SPY only ·{" "}
            {result.start_session} to {result.end_session}.{" "}
            {result.synthetic
              ? "Invented test data."
              : "Source provenance remains unverified."}
          </p>
          {result.trials.map((r: Row) => (
            <article className="broker-order" key={r.name}>
              <strong>{r.name}</strong>
              <span>{r.state}</span>
              <span>
                Modelled net USD: {show(r.report?.trade_statistics.net_pnl_usd)}
              </span>
              <small>
                {r.cost_assumptions.slippage_bps} bps +{" "}
                {r.cost_assumptions.fee_per_share} USD/share per side
              </small>
              <small>
                Delay: {r.fill_assumptions.entry_delay_minutes} min · Full
                spread: {r.fill_assumptions.spread_bps} bps
                {" · "}Delta vs saved USD: {show(r.delta_vs_saved_net_pnl_usd)}
              </small>
            </article>
          ))}
          <p className="caption">
            Four cells rerun sizing and exits. Cost stress adds 2 bps and 0.005
            USD/share per side. Delay waits one minute; combined spread adds 2
            bps per market side. These are assumptions, not measured quotes. No
            order or reservation exists while waiting. No strategy is selected
            or started.
          </p>
          <button onClick={download}>Download SPY study JSON</button>
        </>
      )}
    </details>
  );
}
