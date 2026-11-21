import React, { useState } from "react";
type Row = Record<string, any>;
export default function PaperRecovery({
  recovery,
  changed,
}: {
  recovery?: Row;
  changed: (s: Row) => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [ack, setAck] = useState("");
  const c = recovery?.case;
  async function action(accept: boolean) {
    setBusy(true);
    setError("");
    setAck("");
    try {
      const response = await fetch(
        `/api/paper/recovery/${accept ? "accept" : "prepare"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            accept ? { case_id: c.case_id, fingerprint: c.fingerprint } : {},
          ),
        },
      );
      const value = await response.json();
      if (!response.ok)
        throw new Error(value.detail || "Recovery review failed.");
      changed(value);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Recovery review failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <details>
      <summary>Terminal correction clearance</summary>
      <p className="drawer-note">
        Only reviewed, retired terminal corrections with fresh matching fills
        and a flat account. All held sessions are checked. Fee/cash holds and
        unsupported changes stay blocked. Clearance keeps the worker stopped;
        Start still requires a fresh inventory check. Costs/net profit remain
        unverified.
      </p>
      <button disabled={busy || !recovery} onClick={() => action(false)}>
        Prepare terminal clearance
      </button>
      {c && (
        <article className="broker-order">
          <p>{String(c.assessment.reason).replaceAll("_", " ")}</p>
          <p>
            {c.assessment.sessions.length} session(s) · observed {c.observed_to}
          </p>
          {c.accepted ? (
            <p>
              Acceptance saved. Original holds remain in history; later evidence
              may block again.
            </p>
          ) : (
            <>
              {c.stale && (
                <p className="warning">
                  Evidence changed or expired. Prepare again.
                </p>
              )}
              <label>
                <input
                  type="checkbox"
                  checked={ack === c.case_id}
                  disabled={busy || c.stale || !c.assessment.eligible}
                  onChange={(e) => setAck(e.target.checked ? c.case_id : "")}
                />
                I accept clearance of only these reviewed terminal holds. This
                does not start trading or verify net profit.
              </label>
              <button
                disabled={
                  busy || c.stale || !c.assessment.eligible || ack !== c.case_id
                }
                onClick={() => action(true)}
              >
                Accept terminal clearance
              </button>
            </>
          )}
        </article>
      )}
      {(recovery?.clearances || []).map((v: Row) => (
        <p key={v.seq}>
          Clearance saved {v.accepted_at} · {v.sessions.length} session(s).
          Historical permission; current gates still apply.
        </p>
      ))}
      {recovery?.history_truncated && (
        <p>
          Latest five clearances shown. Full history is in the saved review
          export.
        </p>
      )}
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
    </details>
  );
}
