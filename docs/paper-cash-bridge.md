# Account cash and activity bridge

The native paper database can now retain an opening cash/position anchor and
compare later observations against account-wide activity. This is a diagnostic
step towards recovery reconciliation, not recovery clearance or strategy P&L.

In paper controls, expand **Cash & activity → Read cash & activity**. The first
valid read saves the opening anchor. Later reads keep that same anchor; they
never roll forward past an unexplained interval. The operation only reads the
broker and records local evidence. It cannot start/rearm the worker, release a
reservation, clear a correction/loss hold, cancel or flatten.

## Observations and arithmetic

Two fresh clock/account/position/open-order observations must agree on normalized
inventory and cash. Moving marked equity alone is retained as context. The
account must satisfy the existing USD/active/eligibility validation. Negative
cash and unsupported account formats fail that validation rather than entering
an untested borrowing model. Initial scope binding and the anchor commit
atomically. Reopening never fabricates an anchor for earlier broker history.

Later reads query `/v2/account/activities` for all activity types and all orders,
from one day before the opening read through the closing observation. The query
uses ascending pagination, 100 records/page, at most five pages. No manual or
foreign activity is silently dropped or attributed to ORB. Matching existing
durable order identities can identify owned fills; external fills remain
account-only evidence. Plain US equity/ETF symbols use a declared gross
quantity-times-price cash model. Options, crypto and other unsupported symbols
have unknown cash effects; this does not add those markets to trading scope.

Only fills strictly after the opening observation interval and before the
closing observation interval enter the arithmetic. Earlier fills are excluded;
boundary fills remain ambiguous. Position changes, included signed share changes,
observed cash change and modeled gross fill cash are shown separately. Buy fills
consume cash and add shares; sell fills credit cash and subtract shares. Exact
Decimal arithmetic has no hidden rounding tolerance or balancing adjustment.

Non-trade net amounts retain their sign, date, currency/status availability and
creation time where supplied. They are not silently included in an interval:
a date may describe settlement, and creation time does not prove cash posting
time. Fees, deposits, dividends and corporate actions therefore make interval
timing/effects uncertain. Their observed amounts remain available for inspection.

When scan/timing/effect evidence is incomplete, `unexplained_cash_usd` is null.
`observed_minus_included_fill_cash_usd` remains a diagnostic of the included
subset, never the missing final answer. Arithmetic agreement is labeled
`observed_deltas_agree`; it always retains false settled-cash, fee-coverage and
recovery-clearance flags, with net P&L unavailable. Compensating missing events
could still produce matching deltas. This is not a complete broker ledger.

## Persistence and conservative correction handling

Schema 9 adds a fingerprinted, append-only `paper_cash_log`. Migration from
schema 8 is transactional and preserves all attempts, presets, risk/loss state,
order observations and commands. Network reads do not hold the database writer
lease; committing rechecks the entire saved native basis, including earlier cash
receipts. A changed basis or a late older scan is rejected. Captures, verified
account binding, detected revision/missing-record holds and their receipt commit
together. Actual process-exit tests verify rollback before receipt insertion.

A changed returned activity, disappearance from a later finished scan over the
same expanding creation-time window, or invalid activity evidence creates a
sticky account-wide hold. Previous complete evidence survives intervening failed
reads. Attribution improving from unknown to matched does not masquerade as a
broker revision. These holds feed existing worker/startup risk gates; neither a
matching bridge nor a review clears them. A cash/quantity difference alone is a
diagnostic, not proof of a revision: late posting or limited coverage may explain
it, so it does not automatically create a correction hold.

Reads stop adding inventory requests after 22 elapsed seconds and activity
requests after 52 seconds, with the existing eight-second request timeout.
Captures longer than 60 seconds cannot be eligible comparisons. The window is
bounded to 90 days; at most 200 checks are retained. Capacity creates a sticky
hold without deleting prior evidence. There is deliberately no reset-anchor or
clear-hold control that could discard an unexplained interval. A future reviewed
rollover requires an explicit evidence protocol.

## API, export and acceptance boundary

`POST /api/paper/cash/check` accepts only an empty body with the existing local
Origin/Host checks. `GET /api/paper/cash?after=0` exports 100 receipts/page with a
frozen `through` watermark. Combined paper evidence download v8 adds
`cash_bridges` and `cash_through` as its ninth stream. Streams have separate
watermarks/times, not an atomic cross-stream broker snapshot. Pagination errors
produce no partial download. The on-demand panel preserves the minimal
Workspace/Settings layout, original media, candle emphasis and existing presets.

Reference/MWF/Custom research settings and Calm/Balanced/Active paper settings
are unchanged. No parameter tuning, paid service, news source, actual broker
request or economic result is part of this increment. Remaining acceptance work
includes trustworthy posting/settlement and complete account/activity evidence,
justified correction resolution, integrated recovery clearance and prospective
paper observation. Full research/paper MVP completion is not claimed.

## Source contract checked 14 November 2026

Alpaca's Trading API reference documents creation-time query filters and page
cursors, while its activity-object documentation separates execution time from
non-trade dates/net amounts. These distinctions are why date-only records stay
uncertain instead of being used to force a cash reconciliation:

- https://docs.alpaca.markets/us/reference/getaccountactivities-2
- https://docs.alpaca.markets/us/docs/account-activities

Verification uses injected synthetic clocks/account responses, not credentials
or live provider results. See the PR and separate project report for the actual
checks performed. Mac and CI verification require independent evidence.
