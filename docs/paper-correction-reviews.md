# Correction review records

> New: [Reviewed terminal gross accounting](reviewed-terminal-accounting.md) adds
> a narrowly validated `accept_terminal_accounting` disposition for new case-v2
> records. It creates a separate reviewed gross estimate; all holds and execution
> blocking remain. The original two review dispositions below are unchanged.

Trades → Saved ORB attempts → **Correction review** is available for a held
session. It prepares a bounded read-only account preflight and saves a review
case bound to the retained evidence. The supported decisions are **Keep blocked**
and **Provider review needed**. Both save durable records; neither clears a hold,
changes a fill, changes P&L, starts trading, cancels or flattens anything.

This is a prerequisite for reviewed correction resolution, not a completed
resume/repair workflow. A flat account today does not explain a past correction.
Existing automatic broker actions remain blocked after every review decision.
Inspect Alpaca directly if a real hold occurs; do not delete state to retry.

## Why a new journal?

Earlier releases retained the first hold reason per session. Later disagreements
could be hidden behind it. Schema v4 preserves that existing blocking projection
and adds append-only hold notices, cases and decisions. New order/activity hold
notices retain their source, reason and journal watermarks. Repeated identical
reason/source/watermark combinations deduplicate. These are hold notices, not
individual execution events or a reconstruction of raw broker responses.

Validated v1/v2/v3 databases migrate in one transaction under the same-path worker
lease. Existing holds backfill as **legacy first reason only**; missing history
is not invented. Intents, orders, activities, account scope, session counters and
loss latches remain intact. Interrupted migration rolls back; startup stays
stopped. Append-only triggers and hashes catch accidental edits, not malicious
database-owner rewriting or independent provider authenticity.

## Preflight and evidence binding

Prepare review reads at most two consecutive account/position/open-order passes:
six GETs, no retries, fixed paper host and existing client byte/time bounds.
Account scope must match before inventory reads. Nonempty inventory, exactly 500
orders, malformed responses, blocked account, changed cash/equity or failed reads
stay explicit; no lossy apparently-flat normalization is performed. Nonempty
private holdings/order details are not retained, only counts. Failed reads save
fixed reason codes, never private response bodies or exception text.

Two equal empty passes produce **flat two passes**, a bounded review aid only.
It is not an atomic account snapshot, correction clearance, proof of fee/cash
accounting or proof that an ABA change did not occur between reads. Slow reads
run outside the management/SQLite lock. Before saving, the state-file lease and
current evidence are checked again. A competing conforming worker refuses the
write. Changes during the read save a stale-on-arrival case.

Each case binds fingerprints of all retained session order/activity/scan/hold
evidence, the exact attempt/preset/closing intent, account/risk metadata and all
current account holds. The case also records the preflight interval and bounded
hold history. Integrity failures refuse preparation/review. Changed evidence,
changed metadata, a future clock or age above two minutes makes a case stale.
Preflights exceeding 30 seconds or reversing the clock have explicit states.

A review records the exact case ID, fingerprint, one supported disposition and
server time atomically. Free-text notes/unsupported actions are rejected. A
different decision on the same case conflicts rather than overwriting it.
An exact retry returns its historical decision, even after expiry; it never
refreshes permission. A child-process exit before commit leaves no partial
decision. A later fresh case can retain additional review evidence.

There is no reviewer authentication layer: this is a local user attestation,
not provider confirmation or independent sign-off. Host/Origin/JSON protections
and backend-only credentials remain. No request accepts credentials, remote
endpoints, fill edits, P&L edits or a hold-clear parameter.

## Bounds and export

- Hold journal: 10,000 notices. At capacity the original hold persists and a
  durable history-incomplete flag is set; no silent claim of complete history.
- Review journal: 2,000 case/decision rows. Capacity refuses further review writes
  while preserving holds and prior evidence. Archival remains future work.
- Trades: latest 100 hold notices and five cases per held session, with truncation
  and legacy/incomplete-history labels.
- Download schema v3 includes snapshots, activities, all retained hold notices
  and case/decision records. Each stream has its own frozen sequence watermark;
  they are separately timed, not one atomic provider capture. Failure creates no
  partial download. The report remains a bounded current view.

Local endpoints:

- POST `/api/paper/reviews/prepare`: exactly `{"session":"YYYY-MM-DD"}`.
- POST `/api/paper/reviews/record`: exactly `case_id`, `fingerprint`, `disposition`;
  disposition is `keep_blocked` or `request_provider_review`.
- GET `/api/paper/reviews?after=0`: cases/decisions, 100 rows/page.
- GET `/api/paper/hold-events?after=0`: hold notices, 100 rows/page.

For both exports reuse `through` and `next_after`. Existing report/exports keep
fees/net P&L unavailable; a review does not make held gross estimates eligible.
The minimal interface, presets, fixed Reference and paper rules are unchanged.

## Remaining work and evidence

Terminal correction accounting, authenticated supporting evidence, justified
resolution/reopening policy, fees and atomic integration with the independent
offline ledgers remain unfinished. This release deliberately provides no
execution-permission transition. A future resolution protocol must preserve
history and show why its specific uncertainty has been resolved; a generic
reset/clear button would not do that.

Tests use invented broker responses and a local API, including later hold reasons,
stale cases, concurrency, migration rollback, actual process exit, exact retries,
privacy, capacity and unavailable gross/net outcomes. They do not establish real
provider/account behaviour, Mac/CI compatibility or profitability. No orders are
submitted by this workflow or its development tests.

Primary contracts checked 7 November 2026: [account](https://docs.alpaca.markets/us/reference/getaccount-1),
[open positions](https://docs.alpaca.markets/us/reference/getallopenpositions) and
[orders](https://docs.alpaca.markets/us/reference/getallorders-1).
