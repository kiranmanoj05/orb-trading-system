# Account fee evidence

Trades → Saved ORB attempts → Account fee records reads the paper account's
`/v2/account/activities/FEE` endpoint. It works before an ORB attempt exists.
No order, cancellation, flatten, Start or recovery capability is invoked.

The selected New York **posting-window anchor** is not a trade-session label.
The frozen creation-time query starts 24 hours before midnight at that anchor
and ends at the read's start time. Dates may be today or up to 90 calendar days
old, including weekends. Fees can be posted later than the trade. Ten pages of
100 records are the maximum; a short final page means only that this particular
bounded scan finished. It does not prove all fees or all account activities were
reported. The integration intentionally reads FEE records only.

## Meaning of the amounts

- `signed_cash_amount` preserves the provider's sign: negative is a debit,
  positive is a credit. Amounts are bounded finite decimal strings.
- Missing amount, currency or recognized executed status stays unavailable.
  The account must explicitly report USD, but that **does not fill in a missing
  record currency**. Other currencies remain visible without conversion.
- `observed_signed_usd_cash` sums the current scan's retained records only when
  it finished, all records explicitly have USD/executed/known amounts, and no
  account fee hold exists. Offsetting actual records can sum to zero. An empty
  scan leaves the amount unavailable, rather than claiming zero actual fees.
- Every record remains `account_only_unattributed`. No record is matched by
  symbol, date, proximity or a guessed order ID. Per-trade fees and net P&L remain
  unavailable. This observed cash sum must never be subtracted from one selected
  ORB trade or treated as the account's total costs.

The Trading API's documented nontrade activity shape does not establish a
reliable order/execution linkage. Broker API/SSE examples are different products;
this implementation does not assume their linkage fields exist in this endpoint.
Source: [Alpaca Trading account activities reference](https://docs.alpaca.markets/us/reference/getaccountactivities-2).

## Durability and gates

Paper schema 5 adds one append-only fee journal. Fee versions and exact scan
membership share one transaction with initial account-scope binding and any
sticky account fee hold. A fee read checks scope before network history access
and again inside the commit. Network reads do not hold the management lock or
SQLite transaction. A cooperating worker lease protects the commit. Existing
attempts without a saved account scope cannot be retroactively adopted.

Raw activity/account/order IDs, descriptions, credentials and arbitrary provider
fields are not retained. Activity IDs are hashed; activity date, created time and
first observed time remain distinct. Versions are deduplicated by normalized
content; scan receipts preserve partial failures without exposing exception text.

Changed known amounts/currencies/dates/created times, changes from terminal
statuses, contradictory/repeated pagination, invalid fee evidence, disappearing
records after a finished expanding scan, reversed observation clocks or exhausted
capacity create a sticky account fee hold. Pending or missing details can become
known without erasing the earlier version. An intervening failed scan does not
hide the previous finished scan when checking disappearance. An older late
finishing query for the same anchor cannot replace a newer receipt.

The journal retains at most 12,000 rows, reserving receipt space when possible;
capacity is fail-closed, never silent eviction. Its account hold blocks automatic
broker actions through the existing hold gate. Refresh and restart cannot clear
it. Existing per-session review dispositions cannot clear this account hold.
Every new fee receipt/version supersedes existing evidence-bound review bases,
including accepted reviewed gross projections. This is conservative invalidation,
not a claim that a newly observed fee belongs to that trade. Migration from
schema 4 also supersedes prior review bases; history and holds are preserved.
Stop an existing worker before migration; migration is transactional and starts
no worker automatically.

`POST /api/paper/fees/refresh` accepts exactly `{ "session": "YYYY-MM-DD" }`;
`session` is the anchor date here for compatibility with the evidence commands.
`GET /api/paper/fees?after=0&through=...` exports both fee versions and receipts
with a frozen sequence watermark, 100 rows per page. The combined browser export
is v4 with this fifth independently timed stream. A failed stream produces no
partial download. The on-demand panel shows at most 100 current-scan records.

No actual provider, Mac or CI result is implied by the fake-provider and Chromium
checks. Full fee coverage, per-trade fee attribution, account-wide cash-flow
reconciliation and justified correction recovery remain future milestones.
