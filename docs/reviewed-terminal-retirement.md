# Reviewed terminal intent retirement

> [Reviewed terminal command history](reviewed-management-retirement.md) now
> extends this action to exactly accounted native cancel/close history in
> case-v4. Original receipts remain unchanged and all holds remain. Earlier
> case-v3 retirement without command history keeps its existing contract.

Trades → Saved ORB attempts → Correction review can now **Retire reviewed
terminal intent** for a narrowly supported held attempt. It atomically accepts
the exact terminal gross reconstruction, marks the local intent `done`, releases
its tracked planned headroom and records the decision. It does **not** clear
any correction, account-fee, cash, restart or daily-loss hold. Automatic broker
actions and Start remain blocked. It never submits, cancels or flattens an order.

This completes one local correction/recovery transition, not account-wide
recovery clearance. A flat account alone is insufficient. Do not delete state
or interpret zero planned headroom as reconciled cash, actual exposure or profit.

## Eligible evidence

All [reviewed terminal accounting](reviewed-terminal-accounting.md) conditions
still apply: an active USD account of the saved scope, two agreeing empty
inventory passes, fresh complete terminal unreplaced order evidence, exact
positive entry/exit quantities, selected FILL scan membership and exact
per-order notional/time agreement. Only the existing supported price/time
correction reasons qualify. Missing/ambiguous scans, quantity busts, legacy hold
history and unexplained revisions remain unsupported. The preflight is six
bounded GETs, not an atomic broker snapshot or certified execution history.

Retirement additionally requires:

- A tracked native reservation bound to the unchanged request and saved preset.
  Legacy untracked amounts remain unknown and cannot use this action.
- A pending local attempt. Already completed attempts are not released twice.
- Either no management history (the original pathway), or exact accounted
  native cancel/close/check history under the case-v4 contract linked above.
  The action never discards command outcomes merely because inventory is flat.
- A fresh supported case and exact fingerprint: case-v4 for command history;
  prior case-v3 without commands retains its existing authority. Older contracts
  cannot acquire the new command-review authority. Changed evidence/metadata or an expired/future
  review refuses the action. Cases remain valid for at most two minutes.

Review bases now include observation-head receipts (`last_seen_at`) as well as
append-only event fingerprints. Even an identical repeated snapshot received
during/after preflight invalidates that prepared case. Earlier accepted cases
remain in history, but their old basis cannot become current under the stronger
binding; prepare a fresh case if further review is needed. Nothing rewrites or
retroactively grants authority to old records.

## User flow

1. Refresh owned order evidence and FILL activities for the held session, then
   prepare a fresh correction review. Read the exact assessment and any refusal.
2. If retirement is eligible, acknowledge that the reconstruction and local
   headroom release are accepted while holds and blocked trading remain.
   This acknowledgement is separate from accepting gross accounting alone.
3. Select **Retire reviewed terminal intent**. The response reports the local
   retirement, retained session consumption and persistent holds.

Accounting-only acceptance still leaves the intent/reservation unchanged. If
that decision is already recorded, prepare a new case rather than changing the
old decision. There is no free-text override, clear/reset or resume control.
Errors reset acknowledgement and leave the reservation intact.

## Atomicity and persistence

The existing same-path worker lease and `BEGIN IMMEDIATE` transaction cover:
case freshness/eligibility validation; an accounting record; the terminal risk
release and `state=done`; a linked retirement record; and the final decision.
Review/admission capacity failure, an exception or process exit before commit
rolls back every part. A post-commit restart retains the whole transition.
Concurrent exact decisions create one release. An exact retry returns its
original decision, even after expiry; it never refreshes permission.

The release retains the original reservation, request/preset fingerprints and
terminal observation reference, plus exact review-case ID/fingerprint. Admission
inspection verifies the accounting/retirement/decision links. The retirement
freezes its before/after evidence bases. Later evidence can make the current
gross projection unavailable without erasing the historical retirement. All
holds continue blocking automatic actions after reopen.

No schema-9 table migration is required: case-v3, retirement-v1 and the new
decision payload extend existing append-only logs. The same combined evidence
download includes the new review record and linked release through its existing
review/admission streams and independent frozen cursors. No fake fill, fee, cash
posting, session-cap reset or loss-budget widening occurs. Read-only presets and
their edit/save/duplicate/reset/version workflows are unchanged.

## Remaining acceptance

This is explicit local user acceptance, not authenticated independent review or
provider certification. Hashes/triggers detect local inconsistency; they do not
defend against a database owner rewriting all evidence and hashes. Corrected
terminal retirement does not repair quantity busts, replacements, unmatched
management commands, actual fee attribution, broker net P&L, account-wide
correction clearance or prospective provider acceptance. Those remain separate
work. No live-money orders, paid services or exposed credentials are involved.
