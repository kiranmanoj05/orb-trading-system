# Durable local research archive and queue

The local CLI now uses `results/dashboard-research.sqlite` by default (override
with `--research-db`). Presets keep their separate database. Validated imports,
computed run snapshots, original source-package fields and queued saved-version
experiments survive server restarts. This is historical simulation, not broker
state, paper recovery or provider acquisition.

The dashboard adds **Research jobs**. In Applied settings, save/reset the draft
then **Queue saved version**. The queue freezes the exact copied preset version,
parent run, source identity, Python sources, Python version and relevant runtime
dependency versions at that moment. Editing the preset later cannot alter it.
**Run next queued job** executes at most one job when requested; this release
has no background scheduler. Existing **Apply saved version to new run** uses
the same queue/worker immediately and waits for its result. An identical request
returns its existing job; refreshing does not submit another experiment.

## Evidence and lifecycle

| State | Meaning |
| --- | --- |
| queued | Frozen experiment waiting for explicit execution |
| running | One local worker owns the attempt |
| succeeded | Run snapshot and success record committed together |
| failed | No result published by that attempt; inspect the sanitised reason |
| interrupted | Worker ended before atomic completion; explicit retry is available |

Success describes a completed computation. Missing source minutes, unknown
entries/exits or missing held marks still withhold aggregate performance. An
incomplete evaluation can be a succeeded job. A known no-entry skip can remain
complete and flat. Neither success nor a profile name claims trading edge.

The same evaluator and validated settings run through CLI/API/UI. Reference,
MWF, custom risk/cost/weekday/R/cap settings and old v1/v2/v3 identities remain
unchanged. Timing/stop variants remain unsupported. Stored snapshots retain
their original manifest and outputs after code/dependency upgrades; they are not
silently recomputed. A queued job with a changed engine context fails explicitly.
Queue a new saved-version experiment under the new engine to compare it; retry
never changes the old attempt's engine/configuration.

The archive retains canonical JSON snapshots plus the accepted source-package
fields. Source CSV/capture/bundle strings remain exact; a browser-parsed report
object is retained as canonical JSON, not its original whitespace bytes. Its
uploaded summary remains untrusted. Stored source/run/job digests detect local
inconsistency, not provider authenticity or malicious tamper-proof provenance.
`GET /api/runs/{id}/source` returns the retained source package. Preserve the
original permitted files independently for research reproducibility.

## Transactions, concurrency and interruption

SQLite FULL synchronous transactions commit import source/run/audit together,
and completed result/job status together. A crash before completion leaves no
partially published result. A local POSIX OS file lock serialises evaluation and
recovery across conforming processes using the same authoritative DB path.
The lock is automatically released on process exit. An active worker cannot be
stolen by another worker or recovery request. Do not unlink/replace its lock
file while a worker runs. Cross-host/network filesystems and Windows are not
supported; Linux tests do not certify Mac execution.

After an interrupted process, startup or **Check interrupted worker** marks the
abandoned running attempts interrupted while holding the worker lock. It never
requeues or starts them automatically. **Queue retry** creates a new linked
attempt, preserving the failed/interrupted attempt and the same frozen inputs.
Repeating that retry request returns the same linked attempt. A retry of a
successful, queued or engine-incompatible attempt is rejected. Queued jobs
survive startup unchanged. Result snapshots retain first-write identity/context
when the same deterministic evaluation already exists.

This is a bounded local service. Evaluations run synchronously while requested;
the UI shows working state but has no fabricated progress percentage. There is
no daemon, automatic provider download, auto-optimiser or scheduler, and no
pause/cancel/flatten order control. Separate paper-account reservations and
reconciliation gates are untouched.

## Bounds and operation

Imports still support 10 MiB requests, 20,000 bars and 31 calendar days. The
file-backed archive supports 100 runs, 1000 job records and 256 MiB of retained
source/snapshot/job-input text (not a hard on-disk file-size guarantee). Limits
are checked inside transactions; failed capacity writes roll back. No automatic
pruning or deletion API is provided. The comparison increment adds a checked,
transactional schema-1-to-2 upgrade; account/preset files are untouched. Export/preserve evidence
before deliberate maintenance. Back up a closed database as a file; this release
does not claim online backup, corruption repair or power-loss certification.

For embedded/tests, `create_app(research_db=None)` deliberately uses temporary
memory with a 12-run limit. The UI distinguishes persistent and temporary
storage. CLI/browser tests use explicit temporary paths to avoid cross-test
state. Wrong-schema databases and database symlinks are rejected. Corrupt
archive records fail visibly; they are not silently healed from later data.
Host/Origin, JSON import/settings, size, asset and content-policy guards remain.
Bodyless job commands reject unexpected payload fields. No filesystem path or
external URL supplied in an upload is followed. Only a trusted local user should
access this unauthenticated loopback service.

```sh
python -m orb.dashboard_api --examples --research-db results/dashboard-research.sqlite
python -m orb.research_store --db results/dashboard-research.sqlite jobs
python -m orb.research_store --db results/dashboard-research.sqlite work-one
python -m orb.research_store --db results/dashboard-research.sqlite recover
python -m orb.research_store --db results/dashboard-research.sqlite retry --id JOB_ID
python -m orb.research_store --db results/dashboard-research.sqlite export --id RUN_ID --output run.json
```

CLI exports use exclusive creation and refuse overwrites. No supplied source or
account database is overwritten by export. API routes add `/api/storage`,
`/api/jobs`, `/api/jobs/work-one`, `/api/jobs/recover`, `/api/jobs/{id}/retry`
and `/api/runs/{id}/queue-preset`. They are research-only operations.

Tests cover real server recreation and source/preset/job retention, exact-version
freezing, incomplete/known-skip results, corruption and wrong DBs, budget rollback,
concurrent processes, active-worker recovery exclusion, abrupt exits before/after
commit, result/status atomicity, explicit retry and source/dependency changes.
Browser checks restart the actual server, queue and run jobs, inject a test-only
evaluation failure and retry, preserve earlier runs and check reflow. All prices
are invented acceptance data. No real-market result or provider verification is
claimed. Next: cost sensitivity and chronological comparison/holdout reporting,
plus remaining timing/stop and paper reconciliation acceptance work.


The 31 October increment adds [schedule/cost comparison jobs](schedule-cost-comparisons.md)
to the same worker, with atomic child runs/report publication. Reports explicitly
label retrospective unsealed partitions and unavailable uncertainty/benchmarks.
