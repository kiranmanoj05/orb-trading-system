# Reviewed terminal gross accounting

A held attempt can now produce a **separate, manually accepted terminal gross
estimate** when its retained order and FILL evidence agrees under a narrow policy.
This is accounting review, not correction clearance or permission to trade.
Original holds, fills, account state, counters and loss latches stay unchanged.
The ordinary held gross/net results remain unavailable.

In Trades → Saved ORB attempts:

1. Refresh order evidence, then read fill activities. Their observation order and
   freshness matter; a later order refresh requires a later activity scan.
2. Prepare a correction review. The backend performs the existing bounded
   read-only account preflight, now requiring an explicit USD account currency.
3. Inspect the terminal assessment and its rejection reason or proposed gross.
   Only eligible, current cases offer acceptance.
4. Acknowledge **I accept only this gross reconstruction; trading stays blocked**,
   then **Accept reviewed terminal gross**. The backend rechecks case freshness
   and the exact evidence basis; browser acknowledgement is not an authority gate.

The accepted amount is labelled Reviewed terminal gross. Fees/net P&L remain
unavailable. It is not added to account equity, historical returns, ordinary
closed-trade statistics or execution permission. No order or provider message is
sent. There is no user-supplied price, fee, P&L override or source-version picker.

## Supported policy

The current policy accepts only complete retained hold history whose reasons are
average changed without fill, fill timestamp changed without fill, or changed
FILL activity. It then verifies the actual revision chain: activity changes may
alter price/time only, not identity, role, side or quantity. Other order-history
corrections, replacements, quantity busts, missing prior activities, malformed
evidence, capacity holds and legacy first-reason-only history remain unsupported.

| Requirement | Refusal when absent |
| --- | --- |
| Matching empty active USD account preflight | Flat preflight required; no financial reconciliation inference |
| Current complete bracket and any saved closing order | Missing/head/closing evidence stays unavailable |
| Terminal unreplaced orders, actual positive entry fills and equal exits | Pending, replacement, inconsistent quantity or residual position refuses acceptance |
| Known positive prices and fill times | No zero placeholders or invented timestamps |
| Finished bounded FILL scan with exact activity-version membership | Older receipt without membership, incomplete/ambiguous scan or missing version refuses acceptance |
| Latest retained version of every activity present in the scan | Missing/busted activity is not silently discarded |
| Per-order activity quantities and notionals equal order totals exactly | No rounding tolerance or fee assumption is guessed |
| Per-order last activity time equals the order fill time | Price agreement alone cannot explain a time correction |
| Entry activity times do not follow exit activity times | Chronology stays explicit; tied timestamps do not prove intra-timestamp ordering |
| Order/scan observations within two minutes before preflight, in order | Stale, future or reversed observations refuse acceptance |

At most 200 selected activities enter one assessment. Larger cases require a
future reviewed workflow. Equalities use decimal arithmetic with retained
precision. A provider-rounded cumulative average can therefore refuse acceptance
even when the difference is tiny. This is a limitation, not permission to invent
a tolerance or preferred price.

Gross is sum of accepted sell quantity × price minus buy quantity × price.
Canceled partial entries use actual fills, not requested shares. Negative gross
is retained. Zero-entry attempts do not enter this correction pathway; their
existing known-no-entry semantics are unchanged. Fees, net P&L, account returns
and cash reconciliation are never inferred from FILL records.

## Immutable evidence and persistence

New scan receipts retain the exact matched activity IDs/fingerprints as hashes.
Older receipts remain readable but cannot support this pathway. Case schema v2
freezes selected normalized orders/activities, source references, decimal
comparisons, saved preset and risk metadata. Raw client/account identifiers are
not copied into that context; fingerprints/metadata commitments preserve binding.
Old case-v1 records are unchanged and cannot acquire terminal acceptance.

The existing schema4 append-only review log stores an accounting record and
decision in **one transaction**. Capacity failure or process exit between writes
rolls back both. Exact retries return their immutable prior decision; a different
decision requires a new case. Existing 2,000-row review bounds include accounting
rows. The current projection shows the accepted value only while its basis is
unchanged. Later evidence/metadata supersedes it and makes that projection
unavailable; the original accepted amount remains in the exported journal.

No new SQLite schema migration is needed: versioned case/decision/assessment
payloads extend the existing log. Download v3 already carries the accounting
record with the case/decision stream and its fixed watermark. Other streams keep
their independent times/watermarks. New data never rewrites an old decision.

## Limits and next recovery work

This is explicit local acceptance of an exact gross reconstruction, not provider
certification. A completed creation-time FILL query is not complete execution
history; later corrections, retention and unknown events remain possible. A flat
preflight is bounded quiescence, not an atomic account snapshot. No authentication
or independent reviewer sign-off is implied. Hashes/triggers do not protect
against an owner rewriting data and hashes.

Every correction hold still blocks automatic broker actions and Start, including
after restart. Accounting acceptance does not repair reservations, release risk,
retire an uncertain intent, change a preset or clear a loss latch. Execution
resolution still needs explicit account-wide recovery policy and integration
with the independent coordinator/partial-fill/correction contracts. A generic
clear/reset control remains unavailable.

Fee evidence is not supplied by the selected FILL fields. Historical cost settings
frozen with the preset are assumptions, not broker fee facts. Fee attribution,
net accounting and prospective paper verification remain unfinished. No paid
provider, real-data performance, Mac/CI or actual account verification was used
to develop this feature. Tests use invented prices and injected responses.

Primary source checked 8 November 2026: [Alpaca account activity fields and paging](https://docs.alpaca.markets/us/docs/account-activities).
See [hold/review records](paper-correction-reviews.md) and [reported FILL history](paper-fill-activities.md).
