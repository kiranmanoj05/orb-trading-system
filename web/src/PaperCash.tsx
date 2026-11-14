import React, { useState } from "react";
type Row = Record<string, any>;
const words = (s: string) => s.replaceAll("_", " ");
const usd = (s: string | null | undefined) => s == null ? "Unavailable" : `${Number(s) > 0 ? "+" : ""}${s} USD`;
export default function PaperCash({ cash, changed }: { cash?: Row; changed: (s: Row) => void }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const last = cash?.latest, result = last?.result;
  async function check() {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/paper/cash/check", { method: "POST" });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || "Cash activity read failed.");
      changed(value);
    } catch (e) { setError(e instanceof Error ? e.message : "Cash activity read failed."); }
    finally { setBusy(false); }
  }
  return <details>
    <summary>Cash &amp; activity</summary>
    <p className="drawer-note">Account-wide cash and quantity bridge. First read saves an opening anchor; later reads compare against that same anchor. This does not start the worker, release risk or clear holds.</p>
    {cash?.hold && <p className="warning">Account cash evidence held: {words(cash.hold.reason)}. A matching read cannot clear this hold.</p>}
    {!cash ? <p>Cash evidence unavailable.</p> : !last ? <p>No saved cash anchor yet.</p> : <>
      <article className="broker-order">
        <strong>{words(result.state)}</strong>
        <small>Recorded: {last.observed_at} · opening anchor #{cash.anchor_seq ?? "unavailable"}</small>
        {result.observed_cash_delta_usd != null && <>
          <span>Observed cash change: {usd(result.observed_cash_delta_usd)}</span>
          <span>Modeled gross fill cash: {usd(result.modeled_gross_fill_cash_usd)}</span>
          <span>Cash minus included fills: {usd(result.observed_minus_included_fill_cash_usd)}</span>
          <span>Unexplained cash: {usd(result.unexplained_cash_usd)}</span>
          <small>{result.included_fills} included fills · {result.uncertain_records} uncertain records</small>
          {result.positions?.map((p: Row) => <span key={p.symbol}>{p.symbol} quantity change: {p.observed_quantity_delta} · fill change: {p.fill_quantity_delta} · residual: {p.residual_quantity} shares</span>)}
        </>}
        {last.capture?.scan && <small>Activity scan: {last.capture.scan.scan_finished ? "finished within bound" : "incomplete"} · {last.capture.scan.received} records. A finished scan is not completeness proof.</small>}
        {!last.capture?.snapshot_valid && <span>Inventory: {words(last.capture?.snapshot?.state || "unavailable")}</span>}
      </article>
      {!!last.capture?.events?.length && <details><summary>Observed account activity</summary>
        {last.capture.events.map((e: Row, i: number) => <article className="broker-order" key={`${e.activity_id}-${i}`}>
          <strong>{e.activity_type}{e.symbol ? ` · ${e.symbol} ${e.side}` : ""}</strong>
          <span>{e.activity_type === "FILL" ? `Modeled gross cash: ${usd(e.gross_fill_cash_usd)}` : `Reported signed cash: ${usd(e.signed_cash_amount)} · posting time unavailable`}</span>
          <small>{words(e.attribution)} · {e.transaction_time || e.activity_date || "time unavailable"}</small>
        </article>)}
        {last.capture.events_truncated && <p>First 100 records shown; export retains the bounded scan.</p>}
      </details>}
    </>}
    <button disabled={busy || !cash} onClick={check}>{busy ? "Reading cash & activity…" : "Read cash & activity"}</button>
    {error && <p role="alert" className="warning">{error}</p>}
    <p className="caption">Creation-time history may miss late postings. Dated fees, deposits and corporate actions have uncertain booking times. Agreement is arithmetic only: settled cash, fee coverage and net strategy profit remain unverified. Export history from Trades.</p>
  </details>;
}
