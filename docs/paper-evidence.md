# Saved paper order evidence

> Held sessions now support [correction review records](paper-correction-reviews.md).
> Schema v4 retains later hold notices and evidence-bound preflights/decisions;
> neither review decision clears a hold or resumes trading.

> Update: [Reported fill activities](paper-fill-activities.md) supplements these
> snapshots with read-only individual FILL history, separate quantity/price
> comparisons and a schema-v3 journal. Fees/net P&L and reviewed hold resolution
> remain unavailable. The cumulative snapshots described here are unchanged.

Trades → Saved ORB attempts now preserves local observations linked to the
worker's durable session intent. This is separate from historical simulations,
Alpaca account equity and the latest 500 unattributed account orders.

The active worker captures a normalized entry/bracket/closing snapshot before
acting on observed order state. A POST response alone is not treated as a fill.
Completed attempts stop being polled automatically; use **Refresh evidence DATE**
to re-observe a saved attempt without starting the worker. The refresh checks
account scope and uses only account/order GETs (at most four). It does not submit,
cancel, flatten, change the selected preset or clear a hold. A competing worker's
state-file lease prevents a second process from refreshing through its lock.

## Meaning of the displayed values

| Evidence state | Gross price P&L | Interpretation |
| --- | --- | --- |
| Not observed/order not found | Unavailable | Intent exists; broker outcome has not been established |
| Open or pending | Unavailable | Quantity remains or some orders are nonterminal |
| Incomplete bracket/missing closing order | Unavailable | Required order evidence is absent |
| Missing fill price/zero placeholder | Unavailable | Zero is not invented as a fill price |
| Terminal quantity mismatch | Unavailable | Broker status and ordered/filled quantity disagree |
| Known no entry | $0 | Terminal zero fills, no exit fills; not a closed winning/losing trade |
| Closed price evidence | Gross estimate only | Flat cumulative fills, terminal orders, known entry/exit averages |
| Correction hold | Unavailable | Automatic broker actions are blocked pending inspection |

For a fully closed attempt, gross price P&L is estimated as total sell quantity
× cumulative average sell price minus filled buy quantity × average buy price.
Actual filled quantities, including canceled partial entries, are used. Precision
is retained in decimal strings. Provider averages may already be rounded. Fees,
net P&L, individual executions and account returns are **not** established by
these order snapshots. There is no partial-position P&L, invented fill timestamp,
fee-free net result or reconciliation clearance. Intent-linked attribution is
local matching evidence, not independent verification by Alpaca.

The UI shows the saved preset name/version, session, quantities, status and last
observation time. An absent price stays unavailable. Broker fills from saved
orders can appear on matching paper candles and are de-duplicated against account
observation IDs. Historical markers/range/stop/target overlays remain absent from
paper mode. Current account orders remain visibly unattributed below the journal.

## Journal and recovery

Paper database schema v2 adds append-only normalized snapshots, latest/seen
projections and persistent holds. Existing validated v1 databases migrate in one
SQLite transaction, preserving account scope, attempts, closing intents and loss
latches. Migration refuses an active old worker's lease. Unknown schemas/columns
are refused. Interrupted migration or observation/head writes roll back atomically;
process-exit tests cover the observation/head boundary. Starting the server does
not start trading.

Identical normalized observations do not create duplicate events, but update the
last-seen timestamp. Optional field completion and bracket-leg order changes do
not masquerade as new executions. Snapshots are local observations, not a reconstructed
fill-event stream. Provider fill time and response-completion observation time are
separate. Worker receipt time includes monotonic request duration; test clocks are
explicitly injected for deterministic fixture scenarios.

Quantity regressions, changed known order identity/terms, changed averages or
fill timestamps without more quantity, terminal contradictions, replacements,
unknown statuses and malformed owned-order evidence create a persistent hold.
Previously seen counters/identities are retained across transient missing reads.
A later matching response or restart does not clear the hold. Start is disabled
and automatic broker actions stop; existing broker protection remains as it was.
This is distinct from Stop new entries, which continues ordinary management.
Inspect Alpaca directly if a hold occurs. There is no automatic correction repair
or hold-clear endpoint in this release; preserve the database for a reviewed
correction-resolution workflow. Never delete it to retry an uncertain order.

Hashes and SQLite triggers catch accidental observation edits. They do not prove
provider authenticity or protect against a database owner rewriting data and
hashes. The snapshots remain non-atomic broker reads and do not integrate the
separate offline coordinator/partial-fill/correction ledgers.

## Bounded review and export

The panel shows the latest 100 saved attempts, including legacy attempts without
observations, and flags possible truncation. **Export saved paper evidence**
pages through all retained observations up to a fixed sequence watermark, retaining
explicit unavailable values and known gross estimates. Summary and export have
separate observation times and are not an atomic broker snapshot.

The local GET endpoint is `/api/paper/evidence?after=0`; each response contains up
to 100 observations, `next_after`, `has_more` and `through`. Subsequent pages should
reuse `through`. The POST `/api/paper/evidence/refresh` accepts only an exact saved
`session` date and performs the bounded read-only observation. Both retain the
loopback Host/Origin protections; credentials and account IDs are not exported.
Order identities are represented by stable hashes.

At 10,000 distinct observation events the journal fails closed with a review hold,
rather than silently deleting evidence or continuing unjournaled management.
Repeated identical polls do not consume this bound. Archival/compaction and
reviewed hold resolution remain future work.

Tests use injected broker responses and invented prices. No real order, Mac
runtime, provider entitlement, net performance or account reconciliation is
verified by this feature. The historical Reference, research settings, paper
starter risk limits and explicit Start requirement are unchanged.

References: [Alpaca order object](https://docs.alpaca.markets/us/reference/getorderbyorderid-1)
and [client-order lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid).
Execution-event identifiers/corrections require a separate event-history contract;
cumulative order averages are not substitutes for those events.
