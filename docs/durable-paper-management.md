# Durable paper order management

The native SPY worker now journals cancellation and residual-closing commands
before dispatch. This extends atomic entry admission/terminal release into the
actual management paths. It adds no manual cancel, flatten, retry or hold-clear
API and does not turn Stop new entries into liquidation.

## Cancellation

The worker still evaluates its existing reasons: partial entry, stale entry,
ended exit protection or the five-minutes-before-close deadline. Before DELETE,
it requires fresh retained owned-order evidence and the verified account scope,
checks correction/fee holds and admission integrity, then commits the selected
owned targets as one cancel claim. Raw order IDs are used only for dispatch;
the journal retains hashed IDs, roles, reason, source observation and original
request/preset fingerprints.

Each target is claimed once. A later subset of the same targets cannot produce
another automatic cancellation request. After dispatch the worker records which
requests returned, which had unknown outcomes and which were not attempted after
an earlier failure. A returned DELETE is not proof of cancellation. Subsequent
broker order evidence must show terminal states before residual closing.

A crash after claim but before any DELETE leaves an unknown command. The worker
observes orders but does not replay it. This conservative policy can require
operator inspection even when a request never reached the broker. It prevents
restart from silently treating an uncertain external action as a new command.
Already-held broker protection is not assumed canceled. Stopping entries and
weekday exclusion still allow the existing management rules to run.

## Residual closing inventory

Once the owned bracket is terminal with a positive, consistent remaining
quantity, the worker makes at most eight read-only GETs: two passes over clock,
account, positions and bounded open orders. It requires:

- Matching account scope and explicit USD/active/unblocked account evidence.
- A fresh open-market clock and the current regular exchange session, including
  early-close boundaries. Proof must remain within 30 seconds at commit.
- Exact SPY position quantity equal to the owned residual; no open SPY order.
- Well-formed unique position/order identities and fewer than 500 rows in each
  list; a boundary-sized response is possibly truncated and is refused.
- Two agreeing normalized quantity/identity inventories, including unrelated
  symbols. Unrelated holdings/orders are retained as context and never closed.

Marked account equity and cash are recorded separately, not required to be equal
between passes. Equity normally moves while a position is open; treating that
movement as a position-quantity correction would prevent legitimate closes.
These values are not used to post cash or establish account-wide reconciliation.

Scope mismatch, malformed/incomplete inventory, changed quantities/orders,
closed/stale clocks or network failure never authorise a close. A bounded,
sanitised failure receipt is retained when its original evidence basis remains
valid. No provider exception body or arbitrary payload field is stored.

## Atomic close claim and recovery

The claim transaction rechecks the exact attempt/observation/account-risk basis,
all correction gates, admission integrity, terminal quantity evidence and fresh
inventory proof. The close request is derived from the owned residual and the
existing deterministic `-x` client ID. Its `paper_attempts.closing` update and
append-only close-intent record commit together before POST. No user price,
quantity or endpoint override exists.

The post-dispatch receipt says request returned or outcome unknown. A normalized
returned order is retained only if identity, symbol, side and quantity agree;
unverified bodies remain explicitly unverified. This is not fill confirmation.
Missing acknowledgement after a crash, or a timeout with an existing claim,
never authorises automatic resend. Existing order lookup continues. Partial or
terminal closing failures with remaining quantity still require inspection.

Risk reservations stay fully committed until the separate proof-bound terminal
release. Command receipts cannot release cash/risk, settle fees, clear a loss or
correction hold, or grant recovery permission. An account quantity matching the
owned residual is a bounded condition, not proof that all external activity is
attributed or that cash/fill history is complete. External/manual activity can
still occur between the reads and dispatch; SQLite is not an atomic broker API.

## Persistence and interface

Paper schema 7 adds one append-only management log in the same database. Validated
versions 1–6 migrate transactionally under the worker lease, preserving attempts,
closing requests, admission, holds, loss latches, fees and reviews. Stop the old
worker before migration. Reopen never starts a worker. Old closing requests are
shown as lacking this command journal, and their dispatch history is not invented.
Old cancellation history likewise cannot be reconstructed; claims here describe
commands admitted by this version from current owned-order evidence.

The log is bounded to 12,000 events, with receipt capacity checked before a new
command. Capacity fails closed; events are not silently evicted. A full or failed
receipt write does not erase an already committed command or authorise a retry.
Fingerprints detect local inconsistencies, not an administrator rewriting every
record/hash or authenticated provider provenance. Start/tick validate retained
command links before automatic broker actions. New management evidence also
supersedes existing evidence-bound review cases without clearing their holds.

Settings → Paper trading → Saved order actions shows unknown dispatch, returned
requests, inventory-check outcomes and legacy gaps on demand. No permanent
navigation/status panel or new trading controls were introduced. Preset values,
supported settings, immutable versions and the fixed reference are unchanged.

`GET /api/paper/management` exports 100 events per page with frozen `through` and
advancing `after` cursors. Trades download v6 adds this seventh independently
timed stream; any failed stream prevents a partial download. The interface
shows at most 100 recent management sessions; the export retains bounded history.

## Verification boundary and next work

Tests use injected providers and actual process exits, not real orders. Local
entry/management/release transitions now have durable crash boundaries. Full
account cash/fill/fee reconciliation, reviewed correction clearance, prospective
provider observation and sufficient economic validation remain unfinished. The
independent offline coordinator/reconciliation ledger is still distinct from the
native worker. No fee/net result, Mac, CI or actual provider verification follows
from these tests.
