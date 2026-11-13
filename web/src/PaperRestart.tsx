import React, { useState } from "react";
type Row = Record<string, any>;
export default function PaperRestart({ restart: r, changed }: {
  restart?: Row; changed: (s: Row) => void;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function check() {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/paper/restart/check", { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Restart check failed.");
      changed(result);
    } catch (e) { setError(e instanceof Error ? e.message : "Restart check failed."); }
    finally { setBusy(false); }
  }
  return <details>
    <summary>Restart check</summary>
    <p className="drawer-note">Two broker inventory observations are required before this worker continues after Start. A manual check reads saved state and broker evidence; it does not start or rearm the worker.</p>
    {!r ? <p>Restart evidence unavailable.</p> : <>
      <p>{r.worker_check_required ? "Worker startup check required." : "Worker startup check applied."}
        {r.explicit_restart_required && " Blocked. Use Start to request a fresh worker check."}</p>
      {r.latest ? <article className="broker-order">
        <strong>{String(r.latest.state).replaceAll("_", " ")}</strong>
        <small>Recorded: {r.latest.observed_at}</small>
        <span>{r.latest.snapshot?.passes?.length ?? 0} / 2 inventory observations</span>
        <p>{r.latest.can_continue ? "Inventory agreed at that checkpoint." : "Inventory checkpoint blocked continuation."}
          {!r.basis_matches_current && " Saved state has changed since that checkpoint."}
          {!r.latest_matches_worker_epoch && " This result belongs to a different worker start."}</p>
      </article> : <p>No saved restart checkpoint yet.</p>}
    </>}
    <button disabled={busy || !r} onClick={check}>{busy ? "Checking saved state…" : "Check saved state"}</button>
    {error && <p role="alert" className="warning">{error}</p>}
    <p className="caption">Missing order lookups remain unknown. No command replay, correction clearance or settled-cash reconciliation. Saved checkpoints are not reusable permission after reopening. Export history from Trades.</p>
  </details>;
}
