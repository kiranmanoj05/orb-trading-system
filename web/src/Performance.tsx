import React, { useEffect, useRef, useState } from "react";
type Row = Record<string, any>;
const value = (v: any) =>
  v == null
    ? "Unavailable"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 });
export default function Performance({
  runId,
  revision,
}: {
  runId?: string;
  revision?: string;
}) {
  const [report, setReport] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const path = runId
    ? `/api/runs/${runId}/performance`
    : "/api/paper/performance";
  const epoch = useRef(0);
  useEffect(() => {
    epoch.current++;
    setReport(null);
    setError("");
    setBusy(false);
    return () => {
      epoch.current++;
    };
  }, [path, revision]);
  async function read() {
    const requestEpoch = ++epoch.current;
    setBusy(true);
    setError("");
    setReport(null);
    try {
      const r = await fetch(path),
        data = await r.json();
      if (!r.ok) throw Error(data.detail || "Report unavailable.");
      if (epoch.current === requestEpoch) setReport(data);
    } catch (e) {
      if (epoch.current === requestEpoch)
        setError(e instanceof Error ? e.message : "Report unavailable.");
    } finally {
      if (epoch.current === requestEpoch) setBusy(false);
    }
  }
  function download() {
    if (!report) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2) + "\n"], {
        type: "application/json",
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = runId
      ? "orb-historical-performance.json"
      : "orb-paper-cost-estimates.json";
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <details className="performance-report">
      <summary>{runId ? "Performance report" : "Paper cost estimates"}</summary>
      <button disabled={busy} onClick={read}>
        {busy ? "Calculating…" : "Calculate report"}
      </button>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {report && (
        <>
          {runId ? (
            <>
              <p className="drawer-note">
                Retrospective · {report.session_count} sessions ·{" "}
                {report.metrics_eligible
                  ? "complete within declared scope"
                  : "incomplete evidence"}
                . Profitability is not established.
              </p>
              <dl>
                <dt>Modelled net P&amp;L (USD)</dt>
                <dd>{value(report.trade_statistics.net_pnl_usd)}</dd>
                <dt>Expectancy per closed trade (USD)</dt>
                <dd>{value(report.trade_statistics.expectancy_usd)}</dd>
                <dt>Profit factor</dt>
                <dd>{value(report.trade_statistics.profit_factor)}</dd>
                <dt>95% interval · mean session P&amp;L (USD)</dt>
                <dd>
                  {value(report.session_mean_interval.lower)} to{" "}
                  {value(report.session_mean_interval.upper)}
                </dd>
              </dl>
              {report.session_mean_interval.reason && (
                <p className="caption">
                  {report.session_mean_interval.reason.replaceAll("_", " ")}
                </p>
              )}
              <p className="caption">
                Five-session block resampling, fixed historical sizing. Research
                may include SPY/QQQ; paper execution is SPY only.
              </p>
              <a href={path + "?format=markdown"} download="orb-performance.md">
                Download readable report
              </a>
            </>
          ) : (
            <>
              <p className="drawer-note">{report.notice}</p>
              <dl>
                <dt>Gross fill estimate (USD)</dt>
                <dd>{value(report.totals.gross_price_pnl_usd)}</dd>
                <dt>Modelled fees (USD)</dt>
                <dd>{value(report.totals.modelled_fee_usd)}</dd>
                <dt>Additional cost stress (USD)</dt>
                <dd>{value(report.totals.additional_cost_stress_usd)}</dd>
                <dt>Estimated net P&amp;L (USD)</dt>
                <dd>{value(report.totals.estimated_net_pnl_usd)}</dd>
                <dt>Broker net P&amp;L</dt>
                <dd>Unavailable</dd>
              </dl>
              {report.attempts.map((a: Row) => (
                <p className="caption" key={a.session}>
                  {a.session} · {value(a.estimated_net_pnl_usd)} USD ·{" "}
                  {a.reason?.replaceAll("_", " ") ||
                    "conditional on reported fills and preset cost assumptions"}
                </p>
              ))}
            </>
          )}
          <button onClick={download}>Download report JSON</button>
        </>
      )}
    </details>
  );
}
