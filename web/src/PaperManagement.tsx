import React from "react";
type Row = Record<string, any>;
const words = (v: string) => v.replaceAll("_", " ");
export default function PaperManagement({
  management: m,
}: {
  management?: Row;
}) {
  return (
    <details>
      <summary>Saved order actions</summary>
      <p className="drawer-note">
        Command receipts are not cancellation or fill confirmations. Unknown
        commands are not automatically resent. Stop new entries does not cancel
        or flatten positions.
      </p>
      {!m ? (
        <p>Management evidence unavailable.</p>
      ) : (
        <>
          {!m.attempts?.length && <p>No saved management events yet.</p>}
          {m.attempts?.map((r: Row) => (
            <article className="broker-order" key={r.session}>
              <strong>
                {r.session} · {words(r.state)}
              </strong>
              <span>
                Cancel targets claimed: {r.cancel_target_count} · closing
                quantity:{" "}
                {r.quantity == null
                  ? "unavailable"
                  : `${r.quantity} SPY shares`}
              </span>
              <small>Last recorded: {r.observed_at}</small>
              {r.receipt && (
                <p>
                  Dispatch: {words(r.receipt.outcome)}. Broker confirmation
                  still requires order evidence.
                </p>
              )}
              {r.state.includes("unknown") && (
                <p className="warning">
                  Outcome unknown. Inspect broker orders; this command will not
                  be sent again automatically.
                </p>
              )}
              {r.preflight && (
                <p>
                  Closing inventory: {words(r.preflight.state)} ·{" "}
                  {r.preflight.passes?.length ?? 0} / 2 observations. This does
                  not establish cash reconciliation or recovery clearance.
                </p>
              )}
            </article>
          ))}
          {m.attempts_truncated && (
            <p>
              Latest 100 sessions shown; export retains the bounded journal.
            </p>
          )}
          {!!m.legacy_closing_sessions?.length && (
            <p>
              Older closing claims lack this journal:{" "}
              {m.legacy_closing_sessions.join(", ")}. Their dispatch history was
              not reconstructed.
            </p>
          )}
          <p className="drawer-note">
            Planned reservations and loss/correction holds remain in force.
            Export retained history from Trades → Export saved paper evidence.
          </p>
        </>
      )}
    </details>
  );
}
