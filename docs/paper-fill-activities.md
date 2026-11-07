# Reported paper fill activities

> [Correction review records](paper-correction-reviews.md) now preserve later hold
> reasons and bounded preflight/decision evidence. Reviews keep holds in force;
> fee accounting and justified resolution remain unfinished.

Trades → Saved ORB attempts → **Read fill activities DATE** adds a read-only
history view alongside saved cumulative order observations. Refresh order
evidence first so durable entry/bracket/closing identities can be matched.
Other account orders are counted in the bounded scan but not retained or
claimed as ORB fills.

The original **Closed gross estimate** uses cumulative order averages. The
separate **Activity gross estimate** sums reported FILL quantity × price,
sells minus buys. Each activity shows its provider transaction time and local
first-observed time. Observation time is not treated as execution time. Both
estimates are gross; fees and net P&L remain unavailable.

## Evidence states

| Activity state | Meaning |
| --- | --- |
| Not observed | No saved activity scan; no zero-profit assumption |
| Scan incomplete | Failed read, invalid page, repeated cursor or page bound; retained events are partial evidence |
| Order evidence unavailable | Current order snapshot cannot support comparison |
| Quantity mismatch | Activity sums differ from current order totals; delayed/missing evidence is possible |
| Open or incomplete orders | Quantities may agree, but closure is not established |
| Known no entry | Terminal zero order fills with no matched activities; gross 0, not a closed win/loss |
| Closed quantities agree | Terminal flat priced orders and activity quantities agree; activity gross estimate is available |
| Correction hold | Changed/missing prior activity or existing hold; gross unavailable and automatic actions blocked |

Quantity agreement is **not** account reconciliation or execution completeness.
Order averages can be rounded, and event prices can differ. Both averages remain
visible; neither is overwritten and monetary agreement is not asserted. Unknown
fees are not replaced by historical fee assumptions. No partial-position P&L,
return, expectancy, R or fee allocation is inferred from this view.

## Read policy and limits

The backend checks the saved hashed account scope, then reads the fixed paper
`/v2/account/activities/FILL` resource: ascending pages of 100, at most 10 pages
plus one account GET, no retries. Each response inherits the paper client's 2 MB
and 8-second request limits. The `after` bound is 24 hours before the saved
session's New York midnight; `until` is frozen before the first page. Later pages
reuse both bounds and the last raw activity ID as cursor. Raw IDs, account IDs
and private fields are not saved/exported; stable hashes support local matching.

Alpaca documents `after`/`until` as **creation-time** filters, distinct from
transaction time. A short final page means this query finished, not that every
execution or later correction has been observed. Delayed creation, retention,
later corrections and out-of-window records are not ruled out. A full tenth page
stays incomplete. Repeated activities/cursors cannot certify completion. New
closing identities learned while reading need a later refresh.

Network reads run outside the SQLite/worker management lock. Management can
continue while a history request waits. Before saving, the refresh rechecks the
lease, saved attempt and account scope. A competing conforming process refuses
the refresh; scope changes refuse its result. A late-finishing older query cannot
replace a newer saved scan. This does not make provider reads atomic.

The read does not submit, cancel, flatten, start trading, change presets or clear
a hold. Missing activity/quantity disagreement alone stays unavailable. Changed
values for a known activity ID, contradictory duplicates or a previously seen
activity missing from a finished expanding query create a durable hold. Original
and changed normalized versions are retained; identical retries do not duplicate
events. Later matching responses and restart cannot clear disagreement. Direct
broker inspection and a future reviewed resolution workflow are required; never
delete state to retry an uncertain order.

## Persistence and export

Schema v3 adds append-only activity versions and scan receipts. Validated v1/v2
databases migrate transactionally with the worker lease, retaining attempts,
observations, account scope and daily-loss/correction latches. Interrupted upgrades
roll back. Activity versions and scan receipts commit together; a process-exit
test covers that boundary. Startup remains stopped.

Each new journal is capped at 10,000 rows. Capacity fails closed with a durable
hold; evidence is not silently deleted. The panel shows the first 100 activity
versions per attempt and flags truncation. Export saved paper evidence uses
download schema v2: order snapshots and activity versions each use a separate
fixed sequence watermark, up to 100 pages of 100 rows. Failure creates no partial
download. Report and streams are separately timed, not an atomic broker snapshot.

Local API:

- POST `/api/paper/activities/refresh` with exactly `{"session":"YYYY-MM-DD"}`.
- GET `/api/paper/activities?after=0`, then reuse returned `through` and
  `next_after` for up to 100 activity versions per page.
- `/api/paper/status` includes each attempt's `fill_activities` comparison.

Loopback Host/Origin guards and backend-only credentials remain. Hashes and
append-only triggers detect accidental edits, not provider authenticity or
malicious owner rewriting. Failed scans retain fixed sanitized reason codes and
do not erase earlier evidence.

## Scope and verification

This is reported execution-activity evidence, not a completed fee ledger,
automatic correction resolution, account-wide atomic recovery or provider
certification. Independent offline coordinator/partial-fill/correction contracts
remain separate. Presets, supported research timing/stops, fixed Reference and
bounded SPY paper rules are unchanged. No settings were tuned, orders submitted
or provider outcomes verified in development.

Tests use invented activities and injected HTTP responses: partials, pagination
failure, revisions/missing events, scope/lease races, migration rollback, process
exit, bounded exports and UI unavailable states. They do not establish real
Alpaca timing, completeness, fees or profitability.

Sources checked 6 November 2026: [Alpaca account activities](https://docs.alpaca.markets/us/docs/account-activities)
and [Trading API pagination/filter contract](https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1).
The [order-snapshot contract](paper-evidence.md) remains applicable.
