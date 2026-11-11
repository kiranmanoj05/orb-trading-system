# Atomic paper admission

The native SPY paper worker now commits its entry intent and planned risk
reservation in one SQLite transaction. This is actual worker integration, not a
new research simulator or a permission to clear correction holds.

Before the broker POST, `PaperAdmission.reserve` rechecks the account scope,
correction/fee holds, daily-loss latch and tightened budget, session attempt cap,
and every nonterminal account attempt under `BEGIN IMMEDIATE`. It independently
sizes the supplied limit/stop against the frozen settings and observed allocation,
requires the exact saved settings fingerprint and supported paper policy, then
inserts both the attempt and append-only reservation. A failed commit sends no
order. A timeout after commit keeps both; the worker looks up the deterministic
client ID and never automatically resends it. Separate connections cannot consume
the same session or multiply the pending account budget.

A risk checkpoint atomically persists the observed-equity anchor, monotonically
tightening daily-loss budget and irreversible session loss latch. Checkpoints
can precede a candidate, so a loss breach persists even when bars are missing or
there is no breakout. The claim transaction rechecks these gates after network
sizing work. Half-written older anchor/budget pairs fail closed rather than
inventing the missing value. A new candidate cannot widen a saved budget or
clear a latch by selecting a different profile.

## What is reserved

Each reservation freezes the full saved preset/settings fingerprint and version,
request fingerprint, feed, observation, allocation, limit/stop/target, quantity,
binding sizing reason and budgets. Cash, exposure and planned stop-risk headroom
include the existing configured slippage/per-share fee assumptions used by the
shared sizing engine. Values are decimal strings in USD. These assumptions do
not become actual execution prices or observed broker fees.

One whole reservation consumes the sole supported paper concurrency slot,
including unacknowledged submissions and partial fills. Partial fills do not
release the unfilled portion or allow another profile to reuse it. One attempted
entry consumes the session cap even if it later has a known zero fill. Caps are
ceilings, never quotas. Paper remains one SPY attempt/position under the tested
reference timing/midpoint stop; historical variants or simultaneous paper profiles
are not enabled by this change.

## Terminal release

The worker's terminal path now commits `state=done` and its release record together.
It first requires retained normalized order evidence: terminal, unreplaced orders,
consistent filled quantities, required bracket/closing evidence and exactly zero
remaining ORB quantity. The release binds that observation's sequence/fingerprint
and the original request/preset. Missing proof, conflicting evidence, a reversed
release timestamp or capacity failure leaves the attempt and reservation active.

A known no-fill terminal outcome releases headroom but keeps the session consumed.
Terminal quantity evidence can release headroom without prices; that does not
make gross/net profit known. A later broker correction can still create a hold;
a past release is not a broker certification. Release does not post cash, realise
account P&L, clear loss/correction holds, reconcile external positions, or grant
recovery permission. Start and each worker tick validate the admission ledger
before automatic broker reads/actions.

## Migration, inspection and export

Schema 6 adds one append-only `paper_admission_log` to the existing paper database,
under the existing worker lease and transactional migration. Versions 1–5 remain
recognized. No historical reservation is reconstructed: older pending attempts
have unknown headroom; older completed attempts are counted as untracked history.
Their existing order management still requires evidence. A supported legacy
terminal completion records that no original reservation exists.

Stop the old worker before upgrading. Reopen does not start a worker. Preserved
attempts, holds, fees, reviews, account scope and loss latches are not reset. The
new account evidence commitment supersedes older review bases; history remains.

Settings → Paper trading → Saved entry risk shows actual persisted planned
headroom, pending/consumed counts, loss checkpoint and exact active preset versions.
It is on demand within the existing minimal interface. An unavailable legacy
amount stays unavailable, rather than a guessed zero.

`GET /api/paper/admissions` exports 100 append-only events per page with `after`
and frozen `through` cursors. The combined Trades download is v5 and adds this
sixth independently timed stream. It creates no partial file if a stream fails.
The admission log retains at most 12,000 rows and reserves the last available row
for terminal release rather than another entry. Capacity fails closed; history
is never silently removed. This is a local tamper/error-detection boundary, not
an authenticated broker audit or a defence against an administrator rewriting
all records and hashes.

## Remaining boundary

The external clock/account/inventory/quote reads are not an atomic broker snapshot.
A local transaction cannot prevent an external/manual order or cash change after
the read. The observed equity checkpoint is not a daily opening NAV, and its
budget is not a guaranteed loss bound. Reserved headroom is not broker cash,
marked holdings, reconciled exposure or a settled-fee ledger. Keep the existing
flat-inventory, freshness, quote, correction and reconciliation gates.

This advances the research audit's atomic integration finding in the native paper
entry/terminal paths. Full account-wide cash/fill/correction reconciliation,
justified recovery clearance, per-trade fees/net accounting and actual forward
provider validation remain unfinished. The independent offline coordinator and
reconciliation ledgers are not silently attached to the native worker by this
change. No actual provider orders, Mac or CI result is implied by fixture tests.
