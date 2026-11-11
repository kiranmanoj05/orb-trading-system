import React from "react";
type Row = Record<string, any>;
const usd = (v: any) => (v == null ? "Unavailable" : `${v} USD`);
export default function PaperAdmission({ admission: a }: { admission?: Row }) {
  return (
    <details>
      <summary>Saved entry risk</summary>
      <p className="drawer-note">
        Planned admission headroom, not broker cash, holdings or reconciled
        exposure. Partial fills and uncertain submissions retain the full
        reservation until terminal quantity evidence. Planned risk is not a loss
        guarantee.
      </p>
      {!a ? (
        <p>Admission evidence unavailable.</p>
      ) : (
        <>
          <p>
            Pending reservations: {a.active_count} · session attempts:{" "}
            {a.session_attempt_count}{" "}
            {a.session ? `(${a.session})` : "(no checkpoint)"}
          </p>
          <p>
            Cash headroom: {usd(a.cash_headroom_usd)} · exposure headroom:{" "}
            {usd(a.exposure_headroom_usd)} · planned stop risk:{" "}
            {usd(a.planned_stop_risk_usd)}
          </p>
          <p>
            Observed equity anchor: {usd(a.anchor_equity_usd)} · daily-loss
            budget: {usd(a.loss_budget_usd)} · loss gate:{" "}
            {a.daily_loss_blocked
              ? "entries blocked"
              : a.session
                ? "not latched"
                : "unavailable"}
          </p>
          {a.legacy_pending_sessions?.length > 0 && (
            <p className="warning">
              Older pending attempts have unknown admission amounts:{" "}
              {a.legacy_pending_sessions.join(", ")}. Their risk was not
              reconstructed.
            </p>
          )}
          {a.active_reservations?.map((r: Row) => (
            <p key={r.seq}>
              {r.preset_name} v{r.preset_version} · {r.session} · {r.quantity}{" "}
              SPY shares · planned risk {usd(r.planned_stop_risk_usd)} / budget{" "}
              {usd(r.risk_budget_usd)} · {r.sizing_reason?.replaceAll("_", " ")}{" "}
              · saved {r.observed_at}
            </p>
          ))}
          <p className="drawer-note">
            Terminal release does not post realised cash or clear
            correction/recovery holds. Fees/net P&amp;L remain unavailable.
            Export admission history from Trades → Export saved paper evidence.
          </p>
        </>
      )}
    </details>
  );
}
