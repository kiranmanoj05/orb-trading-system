import React, { useEffect, useState } from "react";

type Settings = Record<string, string | number[]>;
function editable(s: Settings): Settings {
  return {
    ...s,
    schema_version: "orb-research-settings-v3",
    max_session_entries: s.max_session_entries || "2",
    max_concurrent_positions: s.max_concurrent_positions || "2",
    target_r: s.target_r || "2",
    weekdays: Array.isArray(s.weekdays) ? [...s.weekdays] : [0, 1, 2, 3, 4],
  };
}
type Preset = {
  id: string;
  version: number;
  name: string;
  settings: Settings;
  settings_sha256: string;
  readonly: boolean;
};
type Contract = {
  defaults: Settings;
  fields: {
    key: string;
    label: string;
    minimum: string;
    maximum: string;
    zero_allowed: boolean;
    minimum_inclusive?: boolean;
    integer?: boolean;
  }[];
  weekdays: { value: number; label: string }[];
  unsupported: string[];
  notice: string;
};
async function request(path: string, body?: unknown) {
  const response = await fetch(
    path,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || "Preset operation failed.");
  return value;
}
export default function Presets({
  runId,
  applied,
  onApplied,
}: {
  runId?: string;
  applied?: { id: string; version: number; name: string };
  onApplied: (id: string) => Promise<void>;
}) {
  const [list, setList] = useState<Preset[]>([]),
    [contract, setContract] = useState<Contract | null>(null),
    [saved, setSaved] = useState<Preset | null>(null),
    [draft, setDraft] = useState<Settings>({}),
    [name, setName] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [durable, setDurable] = useState(false),
    [history, setHistory] = useState<Preset[]>([]);
  function load(p: Preset) {
    if (p.id !== saved?.id || p.version !== saved?.version) setHistory([p]);
    setSaved(p);
    setDraft(editable(p.settings));
    setName(p.name);
    setNotice("");
    setError("");
  }
  async function refresh(chosen?: Preset) {
    const data = await request("/api/presets");
    setList(data.presets);
    setContract(data.contract);
    setDurable(data.durable);
    if (chosen) load(chosen);
    else if (!saved)
      load(
        data.presets.find((p: Preset) => p.id === applied?.id) ||
          data.presets[0],
      );
  }
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!saved) return;
    let current = true;
    request(`/api/presets/${saved.id}/history`)
      .then((value) => {
        if (current) setHistory(value);
      })
      .catch((e) => {
        if (current) setError(e.message);
      });
    return () => {
      current = false;
    };
  }, [saved?.id, saved?.version]);
  const dirty =
    !!saved &&
    (name !== saved.name ||
      JSON.stringify(draft) !== JSON.stringify(editable(saved.settings)));
  async function action(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Preset operation failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel preset-panel" aria-label="Research preset editor">
      <div className="section-head">
        <div>
          <span className="eyebrow">Versioned research controls</span>
          <h2>Build a deliberate experiment.</h2>
        </div>
        <span className="pill">
          {durable
            ? "Presets saved to local database"
            : "Temporary preset store"}
        </span>
      </div>
      {error && (
        <p role="alert" className="warning">
          {error}{" "}
          <button
            onClick={() =>
              action(async () => {
                const data = await request("/api/presets");
                setList(data.presets);
                setContract(data.contract);
                setDurable(data.durable);
                const latest = data.presets.find(
                  (p: Preset) => p.id === saved?.id,
                );
                load(latest || data.presets[0]);
              })
            }
          >
            Reload saved version
          </button>
        </p>
      )}
      {!contract || !saved ? (
        <p role="status">Loading presets…</p>
      ) : (
        <>
          <p>{contract.notice}</p>
          <div className="view-controls">
            <label>
              Saved preset
              <select
                aria-label="Saved preset"
                value={saved.id}
                disabled={busy}
                onChange={(e) =>
                  load(list.find((p) => p.id === e.target.value)!)
                }
              >
                {list.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · v{p.version}
                    {p.readonly ? " · fixed preset" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Inspect saved version
              <select
                aria-label="Inspect saved version"
                disabled={busy}
                value={saved.version}
                onChange={(e) =>
                  load(
                    history.find((p) => p.version === Number(e.target.value))!,
                  )
                }
              >
                {history.map((p) => (
                  <option key={p.version} value={p.version}>
                    v{p.version} · {p.name}
                  </option>
                ))}
              </select>
            </label>
            <span className="pill" role="status">
              {dirty ? "Unsaved draft" : "Draft matches saved version"}
            </span>
          </div>
          <div className="inset">
            <strong>
              Applied to selected run:{" "}
              {applied
                ? `${applied.name} · v${applied.version}`
                : "Original imported settings (no saved preset)"}
            </strong>
            <p>
              Loading or saving a preset does not change this run. Apply a saved
              version to evaluate a new run. Existing runs retain their exact
              applied version.
            </p>
          </div>
          <label>
            Preset name
            <input
              aria-label="Preset name"
              maxLength={80}
              value={name}
              disabled={saved.readonly || busy}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <fieldset disabled={saved.readonly || busy} className="preset-fields">
            <legend>
              {saved.readonly
                ? "Fixed preset · duplicate to edit"
                : "Draft strategy, risk & cost settings"}
            </legend>
            {contract.fields.map((f) => (
              <label key={f.key}>
                {f.label}
                <input
                  aria-label={f.label}
                  inputMode={f.integer ? "numeric" : "decimal"}
                  value={
                    typeof draft[f.key] === "string" ? String(draft[f.key]) : ""
                  }
                  onChange={(e) =>
                    setDraft({ ...draft, [f.key]: e.target.value })
                  }
                />
                <small>
                  {(f.minimum_inclusive ?? f.zero_allowed)
                    ? "Minimum"
                    : "Greater than"}{" "}
                  {f.minimum}; maximum {f.maximum}
                  {f.key.includes("fraction") ? " · 0.005 = 0.5%" : ""}
                </small>
              </label>
            ))}
          </fieldset>
          <fieldset
            disabled={saved.readonly || busy}
            className="weekday-fields"
          >
            <legend>Eligible entry weekdays · New York session dates</legend>
            <div className="controls">
              {contract.weekdays.map((day) => (
                <label key={day.value} className="check">
                  <input
                    type="checkbox"
                    aria-label={`Eligible ${day.label}`}
                    checked={
                      Array.isArray(draft.weekdays) &&
                      draft.weekdays.includes(day.value)
                    }
                    onChange={(e) => {
                      const days = Array.isArray(draft.weekdays)
                        ? draft.weekdays
                        : [];
                      setDraft({
                        ...draft,
                        weekdays: (e.target.checked
                          ? [...days, day.value]
                          : days.filter((d) => d !== day.value)
                        ).sort(),
                      });
                    }}
                  />
                  {day.label}
                </label>
              ))}
            </div>
            <p className="caption">
              Eligible means allowed to seek a setup, not guaranteed to trade.
              Holidays do not roll forward. Valuation and position management
              continue on inactive days.
            </p>
          </fieldset>
          <div className="controls preset-actions">
            <button
              disabled={busy || saved.readonly || !dirty}
              onClick={() =>
                action(async () => {
                  const p = await request("/api/presets", {
                    id: saved.id,
                    expected_version: saved.version,
                    name,
                    settings: draft,
                  });
                  await refresh(p);
                  setNotice(
                    `Saved ${p.name} v${p.version}. Existing runs unchanged.`,
                  );
                })
              }
            >
              Save new version
            </button>
            <button disabled={busy || !dirty} onClick={() => load(saved)}>
              Reset draft
            </button>
            <button
              disabled={busy}
              onClick={() =>
                action(async () => {
                  const p = await request(
                    `/api/presets/${saved.id}/duplicate`,
                    {
                      version: saved.version,
                      name: `${saved.name.slice(0, 65)} copy`,
                    },
                  );
                  await refresh(p);
                  setNotice(
                    "Duplicated saved version. Edit the new preset to create your variant.",
                  );
                })
              }
            >
              Duplicate saved preset
            </button>
            <button
              className="primary"
              disabled={busy || dirty || !runId}
              onClick={() =>
                action(async () => {
                  const r = await request(`/api/runs/${runId}/apply-preset`, {
                    preset_id: saved.id,
                    version: saved.version,
                  });
                  await onApplied(r.id);
                  setNotice(
                    `Applied ${saved.name} v${saved.version} to a new historical run.`,
                  );
                })
              }
            >
              {busy ? "Working…" : "Apply saved version to new run"}
            </button>
          </div>
          <div className="controls">
            <button
              disabled={busy || dirty || !runId}
              onClick={() =>
                action(async () => {
                  const job = await request(`/api/runs/${runId}/queue-preset`, {
                    preset_id: saved.id,
                    version: saved.version,
                  });
                  setNotice(
                    `${job.state === "queued" ? "Queued" : "Existing " + job.state + " job for"} ${saved.name} v${saved.version}. Job ${job.id.slice(0, 8)}: inspect Research jobs.`,
                  );
                })
              }
            >
              Queue saved version
            </button>
          </div>
          {dirty && (
            <p className="caption">
              Save or reset the draft before applying. Duplicate uses the saved
              version, not unsaved edits.
            </p>
          )}
          {notice && (
            <p className="notice" role="status">
              {notice}
            </p>
          )}
          <details>
            <summary>
              Saved version fingerprint & unsupported parameters
            </summary>
            <code>{saved.settings_sha256}</code>
            <p>
              Unsupported here: {contract.unsupported.join("; ")}. These have no
              editable controls and are not silently ignored.
            </p>
            <p>
              Presets persist across server restarts when a database is
              configured. Research archives also persist when configured; queued
              jobs pin this exact version and require explicit execution. The
              offline coordinator persists admission counters in its separate
              account journal; external partial fills still require
              reconciliation.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
