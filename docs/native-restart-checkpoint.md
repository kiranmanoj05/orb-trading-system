# Native worker restart checkpoint

The native SPY paper worker now requires a fresh inventory checkpoint before
continuing after Start or process restart. Starting remains explicit. Reopening
the database never reuses a saved green result as permission to trade. This
increment advances recovery; it does not complete the paper MVP acceptance gate.

## What the gate proves

Before any automatic broker mutation, the worker freezes its retained attempts,
order evidence, admission reservations, management claims, holds and review
basis. Two bounded read passes inspect account identity, account eligibility,
clock, positions, open orders and the saved entry/closing identities. Each pass
must independently agree with the owned residual quantity; their normalized
inventory and owned order evidence must also agree with each other. Changing
cash/equity marks are context, not invented cash-ledger reconciliation.

A flat database needs empty broker positions/orders. A pending attempt needs a
saved account scope and its original matching entry. SPY residuals and working
entry/closing orders must match the account inventory. Foreign holdings/orders
remain visible context when managing an owned position; the existing flat-account
new-entry gate still blocks admission. Inactive bracket legs are not required
to appear in a flat open-order listing. The listing boundary of 500 is rejected
as potentially truncated. No missing lookup is interpreted as a canceled order,
no fill, flat position or permission to submit again.

The checkpoint itself never marks an attempt done or releases planned risk.
After an agreeing checkpoint, existing management may observe terminal order
proof and apply its normal durable release rules. Missing entry or claimed
closing orders keep their unknown outcome and reservations. Existing unknown
command claims are never automatically resent.

## Atomicity, bounds and failure behavior

Schema 8 adds an append-only, fingerprinted checkpoint journal. Migration from
schema 7 is transactional and creates no historical checkpoints. Two owned order
captures, any correction hold, verified account binding and the checkpoint are
saved in one immediate transaction. The basis is rechecked before writing; a
concurrent evidence change refuses the checkpoint. A process exit before the
checkpoint insert rolls back those captures/binding. An older overlapping scan
cannot overwrite a more recently started check. Broker calls hold no evidence
lease; the worker's existing outer lock still serializes its own actions.

A check has at most two passes and 14 GETs. Further reads stop after 22 elapsed
seconds; the existing broker request timeout is eight seconds. Results older
than 30 seconds cannot authorize continuation. Any unresolved result stops new
entries and management actions and waits for explicit Start; polling does not
repeat the blocked inventory check. This can leave broker-held exits unattended
by the local worker: inspect the broker if a check blocks. No controls silently
cancel or flatten as part of a restart check.

The journal retains at most 2,000 checkpoints. Exhaustion creates a sticky,
account-wide hold rather than evicting evidence. Correction holds, loss latches,
preset versions, session attempts, concurrency and duplicate-signal claims remain
in force. Neither a successful checkpoint nor a correction review clears holds.

## Local interface and export

In the existing paper controls, **Restart check → Check saved state** sends an
empty-body POST to `/api/paper/restart/check`. It reads evidence and records a
checkpoint; it does not start/rearm a worker. The panel distinguishes a required
worker check, a blocked result, changed saved state and a result from another
worker start. It stays on demand, preserving Workspace/Settings navigation and
the equity/candle-first interface.

`GET /api/paper/restart?after=0` exports 100 records per page with a frozen
`through` watermark. The combined paper evidence download is now v7 and includes
`restart_checkpoints` and `restarts_through`, alongside the seven prior streams.
Each stream has a separate observation time and frozen watermark; the download
is not an atomic broker-account snapshot. Failed or nonadvancing pagination
creates no partial download. Local-origin/host and empty-body checks apply.

## Limits and alternatives

Two agreeing observations reduce a torn-read hazard but are not a broker atomic
snapshot and cannot guarantee that inventory stays unchanged after the reads.
They do not establish settled cash, fees, execution completeness, correction
clearance, exact economic loss, profitability or production recovery acceptance.
An automatic retry/command replay was rejected because a missing response does
not prove an order was never accepted. Reusing a prior green receipt was rejected
because inventory can change while the process is down. Requiring every inactive
nested leg in a flat open-order listing would reject valid held brackets.

Reference/MWF/Custom research settings and Calm/Balanced/Active paper versions
are unchanged. This gate consumes their frozen attempts/reservations rather than
adding unsupported parameters. The ETF research baseline is unchanged. Remaining
recovery work includes correction resolution, account cash/activity reconciliation
and prospective paper observation before declaring the full MVP complete.

Verification uses synthetic clocks and fake broker responses, including actual
child-process exits during transactions. No live provider, Mac, CI or economic
performance result follows from these checks.
