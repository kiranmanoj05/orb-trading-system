import React, { useState } from "react";
import PaperReview from "./PaperReview";
import PaperFees from "./PaperFees";
type Row = Record<string, any>;
const amount = (v: any) =>
  v == null
    ? "Unavailable"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
      }).format(Number(v));
const words = (v: string) => v.replaceAll("_", " ");
export default function PaperEvidence({
  report,
  changed,
}: {
  report?: Row;
  changed: (value: Row) => void;
}) {
  const [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  async function refresh(session: string, activities = false) {
    setBusy(session);
    setError("");
    try {
      const r = await fetch(
        activities
          ? "/api/paper/activities/refresh"
          : "/api/paper/evidence/refresh",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session }),
        },
      );
      const data = await r.json();
      if (!r.ok)
        throw new Error(
          data.detail || "Could not refresh saved order evidence.",
        );
      changed(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Evidence refresh failed.");
    } finally {
      setBusy("");
    }
  }
  async function exportHistory() {
    setBusy("export");
    setError("");
    try {
      let after = 0,
        through: number | undefined;
      const observations: Row[] = [];
      for (let page = 0; page < 100; page++) {
        const r = await fetch(
          `/api/paper/evidence?after=${after}${through === undefined ? "" : `&through=${through}`}`,
        );
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "Evidence export failed.");
        through ??= data.through;
        observations.push(...data.observations);
        if (!data.has_more) {
          const fills: Row[] = [];
          let fillAfter = 0,
            fillThrough: number | undefined,
            fillFinished = false;
          for (let p = 0; p < 100; p++) {
            const response = await fetch(
              `/api/paper/activities?after=${fillAfter}${fillThrough === undefined ? "" : `&through=${fillThrough}`}`,
            );
            const history = await response.json();
            if (!response.ok)
              throw new Error(history.detail || "Fill activity export failed.");
            fillThrough ??= history.through;
            fills.push(...history.events);
            if (!history.has_more) {
              fillFinished = true;
              break;
            }
            if (history.next_after <= fillAfter)
              throw new Error("Activity cursor did not advance.");
            fillAfter = history.next_after;
          }
          if (!fillFinished)
            throw new Error(
              "Activity export exceeded its bound; no partial download was created.",
            );
          const reviews: Row[] = [];
          let reviewAfter = 0,
            reviewThrough: number | undefined,
            reviewFinished = false;
          for (let p = 0; p < 20; p++) {
            const response = await fetch(
              `/api/paper/reviews?after=${reviewAfter}${reviewThrough === undefined ? "" : `&through=${reviewThrough}`}`,
            );
            const history = await response.json();
            if (!response.ok)
              throw new Error(history.detail || "Review export failed.");
            reviewThrough ??= history.through;
            reviews.push(...history.events);
            if (!history.has_more) {
              reviewFinished = true;
              break;
            }
            if (history.next_after <= reviewAfter)
              throw new Error("Review cursor did not advance.");
            reviewAfter = history.next_after;
          }
          if (!reviewFinished)
            throw new Error(
              "Review export exceeded its bound; no partial download was created.",
            );
          const holds: Row[] = [];
          let holdAfter = 0,
            holdThrough: number | undefined,
            holdFinished = false;
          for (let p = 0; p < 100; p++) {
            const response = await fetch(
              `/api/paper/hold-events?after=${holdAfter}${holdThrough === undefined ? "" : `&through=${holdThrough}`}`,
            );
            const history = await response.json();
            if (!response.ok)
              throw new Error(history.detail || "Hold history export failed.");
            holdThrough ??= history.through;
            holds.push(...history.events);
            if (!history.has_more) {
              holdFinished = true;
              break;
            }
            if (history.next_after <= holdAfter)
              throw new Error("Hold cursor did not advance.");
            holdAfter = history.next_after;
          }
          if (!holdFinished)
            throw new Error(
              "Hold export exceeded its bound; no partial download was created.",
            );
          const fees: Row[] = [];
          let feeAfter = 0,
            feeThrough: number | undefined,
            feeFinished = false;
          for (let p = 0; p < 120; p++) {
            const response = await fetch(
              `/api/paper/fees?after=${feeAfter}${feeThrough === undefined ? "" : `&through=${feeThrough}`}`,
            );
            const history = await response.json();
            if (!response.ok)
              throw new Error(history.detail || "Fee export failed.");
            feeThrough ??= history.through;
            fees.push(...history.events);
            if (!history.has_more) {
              feeFinished = true;
              break;
            }
            if (history.next_after <= feeAfter)
              throw new Error("Fee cursor did not advance.");
            feeAfter = history.next_after;
          }
          if (!feeFinished)
            throw new Error(
              "Fee export exceeded its bound; no partial download was created.",
            );
          const url = URL.createObjectURL(
            new Blob(
              [
                JSON.stringify(
                  {
                    schema_version: "orb-paper-evidence-download-v4",
                    report,
                    through,
                    observations,
                    fill_activities: fills,
                    activities_through: fillThrough,
                    correction_reviews: reviews,
                    reviews_through: reviewThrough,
                    account_fee_evidence: fees,
                    fees_through: feeThrough,
                    hold_events: holds,
                    holds_through: holdThrough,
                    notice:
                      "Order observations, FILL activities, unattributed account FEE records and review records; streams have separate times/watermarks. Reviews do not clear holds. No execution completeness, fees, net P&L or account reconciliation is established.",
                  },
                  null,
                  2,
                ),
              ],
              { type: "application/json" },
            ),
          );
          const a = document.createElement("a");
          a.href = url;
          a.download = "orb-paper-evidence.json";
          a.click();
          URL.revokeObjectURL(url);
          return;
        }
        if (data.next_after <= after)
          throw new Error("Evidence cursor did not advance.");
        after = data.next_after;
      }
      throw new Error(
        "Export exceeded the local evidence bound; no partial download was created.",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Evidence export failed.");
    } finally {
      setBusy("");
    }
  }
  return (
    <section aria-label="Saved ORB paper evidence">
      <h3>Saved ORB attempts</h3>
      <p className="drawer-note">
        Durable intent-linked orders. Closed gross estimates use cumulative
        broker averages. Fees and net P&L are unavailable; this is separate from
        backtests and account equity.
      </p>
      {report?.entries_blocked && (
        <p role="alert" className="warning">
          Broker correction hold: automatic broker actions are blocked. Inspect
          Alpaca; refreshing evidence does not clear the hold.
        </p>
      )}
      {!report?.attempts?.length && (
        <p>
          No saved ORB attempts yet. Account orders below are not automatically
          attributed to the bot.
        </p>
      )}
      {report?.attempts_may_be_truncated && (
        <p className="warning">
          Latest 100 attempts shown. Export retains the paginated observation
          history.
        </p>
      )}
      {report?.attempts?.map((a: Row) => (
        <article className="broker-order" key={a.session}>
          <strong>SPY · {a.session}</strong>
          <span>{words(a.state)}</span>
          <small>
            {a.preset_name} v{a.preset_version} · entry{" "}
            {a.entry_filled ?? "unknown"} / exit {a.exit_filled ?? "unknown"}{" "}
            shares
          </small>
          <span>Closed gross estimate: {amount(a.gross_price_pnl_usd)}</span>
          <small>Net P&L: Unavailable · per-trade fees unavailable</small>
          <small>Last observed: {a.observed_at || "not observed"}</small>
          {a.hold_reason && (
            <p className="warning">Review required: {words(a.hold_reason)}</p>
          )}
          {a.hold_reason && (
            <PaperReview
              session={a.session}
              review={a.correction_review}
              changed={changed}
            />
          )}
          <button disabled={!!busy} onClick={() => refresh(a.session)}>
            {busy === a.session
              ? "Reading broker…"
              : `Refresh evidence ${a.session}`}
          </button>
          <button disabled={!!busy} onClick={() => refresh(a.session, true)}>
            {`Read fill activities ${a.session}`}
          </button>
          <details>
            <summary>
              Fill activities ·{" "}
              {words(a.fill_activities?.state || "not_observed")}
            </summary>
            <p className="drawer-note">
              Reported individual fills matched to saved orders. Quantity
              agreement does not prove complete executions or reconciliation.
              Fees and net P&amp;L remain unavailable.
            </p>
            {a.fill_activities?.scan && (
              <p>
                Scan{" "}
                {a.fill_activities.scan.scan_finished
                  ? "finished"
                  : "incomplete"}{" "}
                · observed {a.fill_activities.scan.observed_at} ·{" "}
                {a.fill_activities.scan.pages} page(s)
                {a.fill_activities.scan.failure
                  ? ` · ${words(a.fill_activities.scan.failure)}`
                  : ""}
              </p>
            )}
            <p>
              Activity gross estimate:{" "}
              {amount(a.fill_activities?.gross_price_pnl_usd)}
            </p>
            <p>Reported activities: {a.fill_activities?.event_count ?? 0}</p>
            {a.fill_activities?.events?.map((f: Row) => (
              <p key={f.seq}>
                {f.side} {f.quantity} shares @ {amount(f.price)} ·{" "}
                {f.transaction_time} · first observed {f.first_observed_at}
              </p>
            ))}
            {a.fill_activities?.events_truncated && (
              <p>
                First 100 activity versions shown; export includes retained
                history.
              </p>
            )}
            {a.fill_activities?.orders?.map((o: Row) => (
              <p key={o.order_id}>
                {words(o.role)} · activity {o.activity_quantity} / order{" "}
                {o.snapshot_quantity} shares ·{" "}
                {o.quantities_agree ? "quantities agree" : "quantity mismatch"}{" "}
                · activity average {amount(o.activity_average)} / order average{" "}
                {amount(o.snapshot_average)}
              </p>
            ))}
          </details>
          <details>
            <summary>Order observations</summary>
            {a.orders.map((o: Row) => (
              <p key={o.id}>
                {words(o.role)} · {o.side} · {words(o.status)} ·{" "}
                {o.filled_quantity} @ {amount(o.average_fill_price)} ·{" "}
                {o.filled_at || "fill timestamp unavailable"}
              </p>
            ))}
          </details>
        </article>
      ))}
      <PaperFees fees={report?.account_fees} changed={changed} />
      <button disabled={!!busy || !report} onClick={exportHistory}>
        Export saved paper evidence
      </button>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
