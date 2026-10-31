import React, { useEffect, useState } from "react";

type Job = {
  id: string;
  state: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  result_id: string | null;
  error: string | null;
  engine_current: boolean;
  request: {
    kind: string;
    source_run_id?: string;
    engine: string;
    preset?: { name: string; version: number; settings_sha256: string };
    retry_of?: string;
  };
};
async function request(path: string, post = false) {
  const r = await fetch(path, post ? { method: "POST" } : undefined);
  const v = await r.json();
  if (!r.ok) throw Error(v.detail || "Research operation failed");
  return v;
}
export default function ResearchJobs({
  onOpen,
}: {
  onOpen: (id: string) => Promise<void>;
}) {
  const [jobs, setJobs] = useState<Job[]>([]),
    [durable, setDurable] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  async function refresh() {
    const [rows, storage] = await Promise.all([
      request("/api/jobs"),
      request("/api/storage"),
    ]);
    setJobs(rows);
    setDurable(storage.durable);
  }
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  async function action(path: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await request(path, true);
      setNotice(
        result?.state
          ? `Job ${result.state}.`
          : path.endsWith("recover")
            ? `Marked ${result.interrupted} interrupted jobs.`
            : "No queued jobs.",
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Research operation failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="Research job history">
      <div className="section-head">
        <div>
          <span className="eyebrow">Reproducible local research</span>
          <h2>Keep the evidence. Freeze the experiment.</h2>
        </div>
        <span className="pill">
          {durable ? "Runs & jobs saved locally" : "Temporary research archive"}
        </span>
      </div>
      <p>
        Queue an exact saved version in Applied settings. Run one queued job
        here when ready. This release has no background scheduler or data
        acquisition. Server restarts retain file-backed evidence; interrupted
        work is never silently retried.
      </p>
      <div className="controls">
        <button disabled={busy} onClick={() => action("/api/jobs/work-one")}>
          {busy ? "Working locally…" : "Run next queued job"}
        </button>
        <button
          disabled={busy}
          onClick={() => refresh().catch((e) => setError(e.message))}
        >
          Refresh job history
        </button>
        <button disabled={busy} onClick={() => action("/api/jobs/recover")}>
          Check interrupted worker
        </button>
      </div>
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      {!jobs.length && (
        <p>
          No research jobs yet. Import evidence or queue a saved preset version.
        </p>
      )}
      <div className="research-jobs">
        {jobs.map((job) => (
          <article className="inset" key={job.id}>
            <div className="section-head">
              <strong>
                {job.request.preset
                  ? `${job.request.preset.name} · v${job.request.preset.version}`
                  : "Validated source import"}
              </strong>
              <span className="pill">{job.state}</span>
            </div>
            <p>
              Created {job.created_at} ·{" "}
              {job.engine_current
                ? "Current engine"
                : "Earlier engine snapshot"}
            </p>
            <p className="caption">
              Job {job.id} ·{" "}
              {job.request.kind === "preset"
                ? `Source run ${job.request.source_run_id?.slice(0, 16)}`
                : "Original source retained"}
            </p>
            {job.error && (
              <p className="warning">
                {job.error === "engine_changed"
                  ? "Engine changed. Queue a new saved-version experiment; this attempt was not reinterpreted."
                  : job.error === "worker_interrupted"
                    ? "Worker stopped before atomic completion. No result was published by this attempt."
                    : "Evaluation failed. No result was published by this attempt."}
              </p>
            )}
            <div className="controls">
              {job.result_id && (
                <button
                  disabled={busy}
                  onClick={() =>
                    onOpen(job.result_id!).catch((e) => setError(e.message))
                  }
                >
                  Open result {job.result_id.slice(0, 8)}
                </button>
              )}
              {["failed", "interrupted"].includes(job.state) && (
                <button
                  disabled={busy || !job.engine_current}
                  onClick={() => action(`/api/jobs/${job.id}/retry`)}
                >
                  Queue retry {job.id.slice(0, 8)}
                </button>
              )}
            </div>
            <details>
              <summary>Frozen job identity</summary>
              <pre>{JSON.stringify(job.request, null, 2)}</pre>
            </details>
          </article>
        ))}
      </div>
      <p className="caption">
        Historical simulation only. A succeeded job may contain incomplete
        market evidence and unavailable returns. Job completion is not
        investment performance or paper execution.
      </p>
    </section>
  );
}
