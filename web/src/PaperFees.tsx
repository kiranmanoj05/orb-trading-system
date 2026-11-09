import React, { useState } from "react";
type Row = Record<string, any>;
export default function PaperFees({
  fees,
  changed,
}: {
  fees?: Row;
  changed: (v: Row) => void;
}) {
  const [day, setDay] = useState(() =>
      new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/New_York",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date()),
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function refresh() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/paper/fees/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session: day }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Fee read failed.");
      changed(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fee read failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <details>
      <summary>
        Account fee records ·{" "}
        {(fees?.state || "not_observed").replaceAll("_", " ")}
      </summary>
      <p className="drawer-note">
        Read-only account FEE records, unattributed to trades. Negative cash is
        a debit; positive cash is a credit. No per-trade fees, net profit or
        account reconciliation is established.
      </p>
      <label>
        Posting window anchor (New York date)
        <input
          type="date"
          value={day}
          onChange={(e) => setDay(e.target.value)}
        />
      </label>
      <p className="drawer-note">
        Reads from the day before this date through now, up to 90 days back and
        1,000 records. Fees may post later. This is not a trade-session filter
        or a completeness certificate.
      </p>
      <button disabled={busy || !day} onClick={refresh}>
        {busy ? "Reading fees…" : "Read account fees"}
      </button>
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
      {fees?.hold && (
        <p className="warning" role="alert">
          Account fee evidence hold: {fees.hold.reason.replaceAll("_", " ")}.
          Automatic broker actions remain blocked.
        </p>
      )}
      <p>
        Observed signed USD cash:{" "}
        {fees?.observed_signed_usd_cash == null
          ? "Unavailable"
          : `${Number(fees.observed_signed_usd_cash) > 0 ? "+" : ""}${fees.observed_signed_usd_cash} USD`}
      </p>
      {fees?.scan && (
        <p>
          Scan {fees.scan.scan_finished ? "finished" : "incomplete"} ·{" "}
          {fees.scan.after} to {fees.scan.until} · observed{" "}
          {fees.scan.observed_at}
          {fees.scan.failure
            ? ` · ${fees.scan.failure.replaceAll("_", " ")}`
            : ""}
        </p>
      )}
      {fees?.state === "no_fee_records_observed" && (
        <p>No fee records observed. Actual fees remain unknown.</p>
      )}
      {fees?.events?.map((e: Row) => (
        <p key={e.seq}>
          {e.signed_cash_amount ?? "Amount unavailable"}{" "}
          {e.currency || "currency unavailable"} · {e.status} · activity date{" "}
          {e.activity_date || "unavailable"} · created{" "}
          {e.created_at || "unavailable"} · first observed {e.first_observed_at}{" "}
          · unattributed
        </p>
      ))}
      {fees?.events_truncated && (
        <p>
          First 100 records shown. Export includes retained versions and scan
          receipts.
        </p>
      )}
    </details>
  );
}
