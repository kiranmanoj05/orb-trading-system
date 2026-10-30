# Account-wide entry capacity

Saved research settings v3 add `max_session_entries` and
`max_concurrent_positions`, integer strings `1` or `2`. The existing SPY/QQQ
universe has one candidate per symbol/session; larger settings imply unsupported
opportunities and are rejected. Reference/v1 and MWF/v2 records retain their
original values and hashes. The editor expands their meaning into a v3 draft;
only a deliberate save appends a new version. Loading does not migrate records.
The Reference stays fixed. These are independent research variants, not economic
recommendations or paper activation controls.

## Lifecycle

| Observed state | Session capacity | Concurrent capacity |
| --- | --- | --- |
| Reserved, not filled | Hold one possible entry slot | Hold one slot |
| Modeled full fill | Count once on its NY fill session | Hold one slot |
| Closed | Keep the fill-session count | Release slot |
| Unresolved holding | Keep fill-session count on its original date | Hold slot across sessions |
| Confirmed local withdrawal with no fill | Release pending slot | Release slot |
| External partial fill, partial then cancel, or unknown submission | Retain full modeled reservation and block admissions pending reconciliation | Retain slot |

Pending reservations carried from an older session still hold a possible entry
slot today. They cannot be made harmless by a calendar rollover. A filled
holding from an earlier day consumes concurrency today but not another filled
entry count. Calendar session changes reset the applicable daily count, never
cash/exposure, open holdings or unresolved reservations. Filled entries remain
consumed after same-minute or later exits. Limits are caps, not daily quotas.
A rejected or cancelled candidate is not re-armed; the one-candidate rule and
idempotent attempt history still apply.

`EntryCaps` and `capacity_usage` are shared by chronological portfolio replay and
the durable offline coordinator. Research orders fill immediately under the
existing full-fill model, so research pending states are usually transient.
Existing exits precede new entries; all simultaneous entries precede exits at
the same entry open. This conservative tie policy is unchanged. A future exit
cannot free a slot for an earlier admission. Source observations are never
filtered by a schedule or cap; coverage, held marks and daily loss remain active.
Safety gates retain their existing precedence over cap reasons.

## Atomicity and recovery

`CoordinatorConfig(entry_caps=EntryCaps(1, 1))` freezes caps with the offline
account configuration. One authoritative SQLite file per account scope and a
`BEGIN IMMEDIATE` transaction serialise each command. Deterministic replay
reconstructs fill dates from committed account events, and pending slots from
allocations. There is no separately writable counter to drift from cash/intents.
Reservations, intent effects, the decision and command commit or roll back
together. Two workers cannot both spend the last slot. An exact retry returns
its original decision; another request for the same consumed candidate does not
reserve twice. Cap changes cannot reset a live journal by reopening it.

This is the existing modeled full-fill coordinator, **not** integrated broker
execution. External partial/unknown evidence blocks all admissions and modeled
fill/cancel/close operations. It never converts a cancellation into evidence that
partially filled shares disappeared. The separate reconciliation ledger still
needs atomic integration. No paper orders or profile-specific capital pools are
created; simultaneous paper profiles remain unsupported. Account-wide limits
apply to all candidates submitted to the same coordinator, not one quota per
profile label.

As before, the coordinator pins executable model and configuration identities.
Changing shipped model code makes an older coordinator journal fail closed;
there is no automatic migration or reset. Retain the journal and original code
for audit; do not delete it to bypass this guard. Preset v1/v2 compatibility is
separate and has no database migration. Event replay remains O(history) per
command, so scalability and long-running broker recovery are not claimed.

Inspect an existing capped journal with both explicit flags:

```sh
python -m orb coordinator-inspect --db results/account.sqlite \
  --account-scope paper:example \
  --max-session-entries 1 --max-concurrent-positions 1
```

Other frozen risk/cost values must also match. Omitting both flags selects the
legacy uncapped configuration; supplying only one or mismatching the journal
fails. The existing `orb.presets` CLI template/create/save/run route uses the
same v3 contract as API/UI. Historical evaluation outputs use v7 with explicit
policy/caps; imports validate matching original source and the frozen profile,
then recompute. Default v5 and policy-only v6 evidence remain readable.

## Evidence and limitations

Tests use invented complete/skip/missing data and account commands. They exercise
concurrent connections, abrupt exits before/after commit, failed fill/cancel
transactions, retry/withdrawal/restart, carried pending/unresolved allocations,
external partial/unknown evidence, calendar reset, actual historical cap
rejections, source/profile import and CLI/API effects. The parent default golden
traces are still checked. Browser checks save/apply limits and retain old runs.
These establish bounded software behaviour, not performance, provider/Mac/CI
verification or a guarantee against gap losses.

Presets and offline account commands persist; dashboard imports/evaluations
still clear when its server restarts. Next implement durable historical jobs and
cost/holdout comparisons, then remaining broker reconciliation/recovery and
supported timing/stop variants. Paper submissions stay disabled.
