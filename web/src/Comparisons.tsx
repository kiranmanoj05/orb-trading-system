import React, { useEffect, useState } from "react";
type Row = Record<string, any>;
async function request(path: string, body?: unknown) {
  const r = await fetch(
    path,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const v = await r.json();
  if (!r.ok) throw Error(v.detail || "Comparison operation failed");
  return v;
}
const percent = (v: string | null | undefined) =>
  v == null ? "unavailable" : `${(Number(v) * 100).toFixed(4)}%`;
const number = (v: string | null | undefined) =>
  v == null ? "unavailable" : Number(v).toFixed(4);
export default function Comparisons({
  runId,
  comparisonId,
  onOpen,
}: {
  runId?: string;
  comparisonId?: string;
  onOpen: (id: string) => Promise<void>;
}) {
  const [presets, setPresets] = useState<Row[]>([]),
    [chosen, setChosen] = useState<string[]>(["orb-reference", "orb-mwf"]),
    [name, setName] = useState("Weekday and cost comparison"),
    [split, setSplit] = useState(""),
    [bps, setBps] = useState("2"),
    [fee, setFee] = useState("0.005"),
    [history, setHistory] = useState<Row[]>([]),
    [selected, setSelected] = useState(""),
    [result, setResult] = useState<Row | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  async function refresh() {
    const [p, h] = await Promise.all([
      request("/api/presets"),
      request("/api/comparisons"),
    ]);
    setPresets(p.presets);
    setHistory(h);
  }
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (comparisonId) setSelected(comparisonId);
  }, [comparisonId]);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    request("/api/comparisons/" + selected)
      .then((v) => {
        if (active) setResult(v);
      })
      .catch((e) => setError(e.message));
    return () => {
      active = false;
    };
  }, [selected]);
  async function queue() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const ids = [
        "orb-reference",
        ...chosen.filter((id) => id !== "orb-reference"),
      ];
      const job = await request(`/api/runs/${runId}/queue-comparison`, {
        name,
        split_session: split,
        presets: ids.map((id) => {
          const p = presets.find((p) => p.id === id)!;
          return { id: p.id, version: p.version };
        }),
        costs: [
          { name: "Zero diagnostic", slippage_bps: "0", fee_per_share: "0" },
          {
            name: "Illustrative stress",
            slippage_bps: bps,
            fee_per_share: fee,
          },
        ],
      });
      setNotice(
        `Comparison job ${job.id.slice(0, 8)} ${job.state}. Run it from Research jobs.`,
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Comparison rejected");
    } finally {
      setBusy(false);
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
    a.download = `orb-comparison-${result.comparison_id.slice(0, 12)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <section
      className="panel comparison-panel"
      aria-label="Schedule and cost comparisons"
    >
      <span className="eyebrow">
        Retrospective research · no winner selection
      </span>
      <h2>Compare the same evidence.</h2>
      <p>
        First family: different New York weekdays, identical remaining Reference
        settings. Every profile is fully reevaluated under common cost
        assumptions. Split periods start from separate flat accounts. Imported
        data is already accessible: this does not create an untouched final
        holdout.
      </p>
      <div className="preset-fields">
        <label>
          Comparison name
          <input
            aria-label="Comparison name"
            maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          Validation begins (NY exchange session)
          <input
            aria-label="Validation begins (NY exchange session)"
            type="date"
            value={split}
            onChange={(e) => setSplit(e.target.value)}
          />
        </label>
        <label>
          Stress slippage per market side (bps)
          <input
            aria-label="Stress slippage per market side (bps)"
            inputMode="decimal"
            value={bps}
            onChange={(e) => setBps(e.target.value)}
          />
        </label>
        <label>
          Stress fee per share per side (USD)
          <input
            aria-label="Stress fee per share per side (USD)"
            inputMode="decimal"
            value={fee}
            onChange={(e) => setFee(e.target.value)}
          />
        </label>
      </div>
      <fieldset className="weekday-fields">
        <legend>Saved schedule versions · two or three profiles</legend>
        <div className="controls">
          {presets.map((p) => (
            <label className="check" key={p.id}>
              <input
                type="checkbox"
                aria-label={`Compare ${p.name}`}
                checked={chosen.includes(p.id)}
                disabled={p.id === "orb-reference" || busy}
                onChange={(e) =>
                  setChosen(
                    e.target.checked
                      ? [...chosen, p.id]
                      : chosen.filter((id) => id !== p.id),
                  )
                }
              />
              {p.name} · v{p.version}
            </label>
          ))}
        </div>
      </fieldset>
      <p className="caption">
        Source {runId?.slice(0, 16) || "not selected"}. Both sides need declared
        exchange sessions; a one-session source cannot be split. Costs are
        illustrative, not calibrated. Custom profiles with different risk, R,
        capital or caps are rejected here.
      </p>
      <button disabled={busy || !runId || !split} onClick={queue}>
        {busy ? "Freezing protocol…" : "Queue comparison protocol"}
      </button>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <div className="view-controls">
        <label>
          Saved comparison
          <select
            aria-label="Saved comparison"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="">Select a saved comparison</option>
            {history.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} · {c.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => refresh().catch((e) => setError(e.message))}>
          Refresh comparisons
        </button>
      </div>
      {result && (
        <>
          <h3>{result.manifest.plan.name}</h3>
          <p className="warning">
            Retrospective, unsealed validation · independent flat-account
            restarts. No ranking or performance claim.
          </p>
          <p className="caption">
            Parent {result.manifest.parent_evaluation_id} · cash comparator 0%
            (zero interest/costs).
          </p>
          <div
            className="table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Comparison results table"
          >
            <table>
              <caption>All attempted schedule, cost and period cells</caption>
              <thead>
                <tr>
                  {[
                    "Period / preset",
                    "Costs / state",
                    "Closed / candidates",
                    "Return / sampled drawdown",
                    "Net expectancy R / exposure",
                    "Initial risk use / fees",
                    "Evidence",
                  ].map((h) => (
                    <th key={h}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((r: Row, i: number) => {
                  const m = r.metrics;
                  return (
                    <tr key={i}>
                      <td>
                        {r.period}
                        <br />
                        {r.preset_name} v{r.preset_version}
                        <br />
                        {r.start_session} → {r.end_session}
                      </td>
                      <td>
                        {r.cost_name}
                        <br />
                        {r.cost_assumptions.slippage_bps} bps +{" "}
                        {r.cost_assumptions.fee_per_share} USD/share/side
                        <br />
                        {r.state}
                      </td>
                      <td>
                        {m
                          ? `${m.closed_trade_count} / ${m.candidate_count}`
                          : "unavailable"}
                      </td>
                      <td>
                        {percent(m?.total_return_fraction)}
                        <br />
                        {percent(m?.sampled_max_drawdown_fraction)}
                      </td>
                      <td>
                        {number(m?.expectancy_net_r)} R<br />
                        {m?.expectancy_reason?.replaceAll("_", " ")}
                        <br />
                        {percent(m?.mean_capital_exposure_fraction)} avg capital
                        <br />
                        {m?.sampled_exposure_minutes ?? "unavailable"} sampled
                        minutes
                      </td>
                      <td>
                        {percent(
                          m?.mean_actual_initial_risk_utilisation_fraction,
                        )}
                        <br />
                        {m?.fees_paid_usd ?? "unavailable"} USD
                      </td>
                      <td>
                        {r.run_id ? (
                          <button
                            onClick={() =>
                              onOpen(r.run_id).catch((e) => setError(e.message))
                            }
                          >
                            Inspect trial {i + 1}
                          </button>
                        ) : (
                          "No source bars"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <details>
            <summary>Paired NY sessions, unknown cells and limitations</summary>
            <pre>
              {JSON.stringify(
                {
                  paired_sessions: result.paired_sessions,
                  cost_sensitivity: result.cost_sensitivity,
                },
                null,
                2,
              )}
            </pre>
            <ul>
              {result.limitations.map((s: string) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </details>
          <p className="caption">
            Closed-subset R is separately recorded in the export. Confidence
            intervals and passive/exposure-matched benchmarks remain
            unavailable; these are not independent ETF trade samples.
          </p>
          <button onClick={download}>Export comparison JSON</button>
          <a
            className="pill"
            href={`/api/comparisons/${result.comparison_id}/report`}
            target="_blank"
            rel="noreferrer"
          >
            Open Markdown report
          </a>
        </>
      )}
    </section>
  );
}
