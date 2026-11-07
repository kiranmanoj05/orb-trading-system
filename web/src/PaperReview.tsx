import React, { useState } from "react";
type Row = Record<string, any>;
const words = (v: string) => v.replaceAll("_", " ");
export default function PaperReview({
  session,
  review,
  changed,
}: {
  session: string;
  review?: Row;
  changed: (value: Row) => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function command(kind: string, body: Row) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/paper/reviews/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const value = await response.json();
      if (!response.ok)
        throw new Error(value.detail || "Review could not be saved.");
      changed(value);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Review failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <details aria-label={`Correction review ${session}`}>
      <summary>Correction review</summary>
      <p className="drawer-note">
        Review records keep automatic actions blocked. A flat-account preflight
        does not clear a hold, certify reconciliation or resume trading.
      </p>
      <p>Hold evidence: {review?.hold_history?.event_count ?? 0} event(s)</p>
      {(review?.hold_history?.legacy_history ||
        review?.hold_history?.incomplete) && (
        <p className="warning">
          Earlier hold history is incomplete. Legacy first reasons are retained;
          missing history was not reconstructed.
        </p>
      )}
      {review?.hold_history?.events?.map((h: Row) => (
        <p key={h.seq}>
          {words(h.reason)} · {words(h.source)} · {h.first_observed_at}
        </p>
      ))}
      {review?.hold_history?.truncated && (
        <p>
          Latest 100 hold events shown. Prepared cases flag the bounded history.
        </p>
      )}
      <button disabled={busy} onClick={() => command("prepare", { session })}>
        {busy ? "Reading review evidence…" : `Prepare review ${session}`}
      </button>
      {review?.cases?.map((c: Row) => (
        <article className="broker-order" key={c.case_id}>
          <strong>Preflight: {words(c.preflight.state)}</strong>
          <small>
            Observed {c.observed_from} → {c.observed_to}
          </small>
          <small>
            Case {c.case_id.slice(0, 12)} ·{" "}
            {c.stale ? "stale — prepare again" : "current evidence"}
          </small>
          {c.preflight.passes?.map((p: Row, i: number) => (
            <small key={i}>
              Pass {i + 1}: {p.position_count} position(s), {p.open_order_count}{" "}
              open order(s) · cash ${p.cash_usd} · equity ${p.equity_usd}
            </small>
          ))}
          {c.decision ? (
            <p>
              Recorded: {words(c.decision.disposition)} ·{" "}
              {c.decision.recorded_at}. Hold remains.
            </p>
          ) : (
            <>
              <button
                disabled={busy || c.stale}
                onClick={() =>
                  command("record", {
                    case_id: c.case_id,
                    fingerprint: c.fingerprint,
                    disposition: "keep_blocked",
                  })
                }
              >
                Record keep blocked
              </button>
              <button
                disabled={busy || c.stale}
                onClick={() =>
                  command("record", {
                    case_id: c.case_id,
                    fingerprint: c.fingerprint,
                    disposition: "request_provider_review",
                  })
                }
              >
                Record provider review needed
              </button>
            </>
          )}
        </article>
      ))}
      {review?.cases_truncated && (
        <p>Latest five cases shown. Export includes retained review history.</p>
      )}
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
    </details>
  );
}
