# Reviewed terminal correction clearance

This release candidate adds a real, explicit permission transition under
Settings → Paper trading → Terminal correction clearance. It is separate from
terminal gross accounting and local intent retirement. Those older actions
still retain every hold. Presets, strategy rules, media and Workspace/Settings
navigation are unchanged.

## Conditions

Stop new entries first. Refresh owned order and fill-activity evidence for the
held sessions, accept their exact terminal reconstruction and retire their
native intents. Prepare terminal clearance reads the account, positions and
open orders twice using backend credentials. No order is sent or canceled.

The policy requires all of the following:

- The same saved USD paper account, active and unblocked, with equal cash/equity
  and empty position/open-order inventories in both bounded passes. Preparation
  must finish within 30 seconds. Nonempty or 500-order inventory refuses.
- Every intent terminal, no active reservation or legacy completed attempt, and
  complete retained terminal order quantities across the account. At most 100
  saved attempts and 20 active held sessions are supported.
- Every active held session covered together: tracked reviewed retirement,
  complete supported price/time correction history, fresh order/FILL evidence,
  exact quantities/notionals/times and the current accepted gross reconstruction.
  Source receipts must be no more than two minutes old when preparation begins.
- No account fee, cash, restart-capacity or incomplete-history hold. Replaced
  orders, quantity/identity changes, unknown orders, missing history and
  unsupported correction types refuse rather than gaining generic clearance.

The user separately acknowledges and accepts the exact prepared case. Acceptance
expires after two minutes or any saved account-basis change, including a repeated
order receipt during the review. Caller-supplied eligibility/evidence is rejected.
The clearance and decision append in one SQLite transaction. A capacity failure
or process exit before commit leaves both unapplied. Exact accepted retries do
not write again, even after expiry/reopening.

## What changes

Only the supported session holds become inactive; original hold rows, versions,
review decisions, unknown command receipts and risk journals remain unchanged.
After valid clearance, the activity projection uses only the exact fill versions
selected by the accepted accounting review. It never sums an old and corrected
version as separate fills or silently chooses the latest version. The export
retains all versions, and the report names the selection and original version
count. Conditional cost estimates can use that projection; broker fees/net profit
remain unknown. Invalidated clearance withholds this selection and blocks again.

All session entries stay consumed. Daily-loss anchors, budgets and latches stay
in force. Acceptance leaves the worker stopped and creates no retry authority.
Explicit Start still requires a new worker epoch and fresh inventory checks.
A manual restart check alone does not start/rearm it.

Clearance is bound to each session's retained source identity. A new order
payload, activity scan, management/risk record or another hold occurrence blocks
that session again. Even the same reason after clearance is a new occurrence;
capacity exhaustion cannot erase it. An identical order receipt with unchanged
payload can advance last-seen time without invalidating a historical clearance.
Refreshing old activity scans is intentionally conservative and may require a
new review. Later valid activity in another session does not erase an accepted
historical clearance. New account-level holds always block.

## Evidence and limitations

Recovery cases, clearances and decisions appear in the existing paginated review
export; no new database table/migration or second journal is introduced. Integrity
checks bind case, decision, accounting and retirement/release records. Missing
links fail closed. These local hashes are not authentication against an owner
rewriting the database and every hash.

This is a bounded local entry-permission policy, not certification of the
provider's complete history, actual broker fee attribution, settlement or net
profit. A flat account and matching fill evidence do not prove those claims.
Unattributed fees and conditional cost estimates remain explicitly separate.
Unsupported account recovery remains blocked for investigation; never delete
SQLite, reset anchors or force a command retry to bypass it. No actual provider,
Mac or profitability acceptance is established by synthetic tests.

The API is `POST /api/paper/recovery/prepare` with `{}`, then
`POST /api/paper/recovery/accept` with the exact `case_id` and `fingerprint`.
The existing loopback Host/Origin guards and server-only keys apply.
