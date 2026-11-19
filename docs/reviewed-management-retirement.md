# Reviewed terminal command history

The existing **Retire reviewed terminal intent** action can now account for
supported native cancel/close history. It uses exact later terminal order/FILL
evidence and records that review in the same transaction as accounting,
intent completion, planned-headroom release and the final decision. It never
resends a command, changes a transport receipt or clears an account hold.

This extends [reviewed terminal retirement](reviewed-terminal-retirement.md).
All its terminal accounting, saved scope, tracked reservation, freshness and
loss/session constraints still apply. Start and automatic broker actions remain
blocked by every correction/account hold, including after reopen. Local terminal
history is not current account reconciliation or permission to resume trading.

## What can be accounted for

| Saved history | Required terminal evidence | What remains unknown |
| --- | --- | --- |
| Cancel intent | Every selected owned ID/role appears unreplaced and terminal in the exact accepted order set | Whether this DELETE caused cancellation; a target may have filled first |
| Native close intent | Exact saved market/day sell terms and quantity, one owned terminal closing order, matching returned identity if retained, exact total flat quantity/FILL accounting | Transport timing when no receipt or a timeout was recorded |
| Failed close check | A retained passive check, followed by the otherwise eligible terminal evidence | It never claimed or sent an order and is not relabelled as one |

Cancellation target outcomes (`request_returned`, `outcome_unknown`,
`not_attempted`) and absent dispatch receipts remain explicit. A returned request
is still not a fill/cancellation confirmation. The reviewed conclusion for a
cancel is **targets terminal, cause unattributed**. No command is automatically
retried or reconstructed as having succeeded. A closing lookup that remains
missing cannot use this action, even when current inventory is empty.

The accepted history covers at most 100 management records for the session.
Every record must precede the final retained terminal order receipt and be
chronological. Later receipts, unknown/duplicate target IDs, wrong roles,
ambiguous legacy closing intents, changed closing terms/quantities, mismatched
response identities, replacement evidence and regressed returned fills refuse
acceptance. A query bound is a refusal, never a truncated apparently-valid case.

The exact terminal accounting assessment independently requires positive entry
fills, equal exits, complete unreplaced terminal orders, selected FILL versions,
exact per-order quantity/notional/last-time agreement and bounded flat preflight.
These rules are unchanged. No fee, net-profit, settled-cash or execution-history
completeness claim is derived from transport receipts.

## Persistence and contracts

New case-v4 records freeze command/receipt sequence/fingerprint/time references,
terminal mappings, untouched transport outcomes and the accepted observation.
The management assessment and its accepted `management_resolution` record are
versioned v1; retirement assessments with command history are v2. Existing
case-v3 retirement without command history remains supported. Old case-v3
records cannot gain this new command-review authority; prepare a fresh case.

One existing worker lease and `BEGIN IMMEDIATE` transaction bind accounting,
management-resolution acceptance, terminal release/state, retirement and final
decision. Capacity failure, exception or process exit before commit rolls all of
them back. A post-commit restart retains the entire transition. Concurrent exact
decisions produce one resolution/release. Exact retries return their original
decision without new broker reads, writes or permission.

Admission and management inspection both validate the linked case, accounting,
management-resolution, retirement, decision and original command references.
Missing/cross-linked review records fail closed. Source command journals are
unchanged. No table migration or rewrite of historical receipts is required.
The existing combined evidence export includes the new review kind and linked
admission release, retaining independent frozen stream cursors.

## Dashboard behaviour

Correction review explains command eligibility, command count and unconfirmed
transport count before the existing separate retirement acknowledgement. Saved
order actions shows **reviewed terminal history** alongside the original
transport state and explicit no-resend/no-resume boundary. This is an on-demand
panel; minimal Workspace/Settings navigation, chart, media and appearance remain.

After a saved retirement, the client immediately refreshes full paper status.
Old in-flight status responses are discarded. Derived admission, management,
restart and cash views become unavailable until that fresh read succeeds, so
the previous reservation is not presented as current. A failed refresh stays
explicitly unavailable until a later successful read. The saved evidence remains
visible. No request in this flow starts, cancels or flattens trading.

Reference/MWF/Custom and Calm/Balanced/Active values, immutable versions and
selection/edit/save/duplicate/reset/apply/history are unchanged. The new review
contract is lifecycle evidence, not a strategy parameter or optimised preset.

## Remaining boundary

Review is a local attestation, not independently authenticated provider
certification. Two empty inventory passes remain non-atomic; hashes/triggers
do not protect against an owner rewriting every record and hash. Later evidence
may contradict historical terminal quantities and all holds still apply.
Account-wide hold resolution, quantity-bust/replacement repair, actual fee
attribution, broker net P&L and prospective provider acceptance remain separate
work. No live-money order, paid service or credential exposure is authorised.
