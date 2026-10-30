# Weekday and planned-target research variants

ORB Reference is unchanged. ORB MWF is an independent, transparent entry-calendar
hypothesis. It is not a reconstruction of Hunter/Safe Flow or a performance/safety
claim. All other built-in risk/cost/signal rules match Reference. Custom saved
versions can vary weekdays and planned target R; unsupported settings still fail.

## Entry policy

A validated immutable EntryPolicy travels through detector → chronological
replay → fill simulator → shared portfolio → evaluator. Default callers retain
all eligible exchange weekdays and 2R. Non-reference policies have distinct
strategy/signal identities and an explicit manifest configuration. This prevents
variants from masquerading as the fixed baseline. The three published-parent
complete/known-skip/missing-entry acceptance cases retain identical hashes for
summary, portfolio, coverage and valuation under default or explicit Reference.

JSON `weekdays` is a nonempty, ordered, unique list of integers 0–4 (Mon–Fri).
Booleans, duplicates, weekends, strings, unsorted/empty selections are rejected.
Eligibility uses **America/New_York session dates**, then the existing XNYS
calendar. Holidays are closed; no opportunity rolls into another day. Early-close
flatten deadlines remain exchange-derived. Eligible days seek a setup; they do
not promise or force a trade.

The schedule gates **new candidates only**. No source bars are removed from
valuation, shared-account observations or daily-loss checks. Existing simulated
exposure stays allocated on inactive days. Missing held marks on an inactive day
still invalidate valuation and latch loss/data guards. All requested coverage,
including inactive days, remains in the completeness denominator. A truncated
inactive day cannot silently become a complete flat return. This does not add
cross-session recovery or repair unresolved positions: those existing limitations
remain, and paper position-management integration is still separate work.

Inactive observed symbol/sessions have state `inactive` and reason
`weekday_not_eligible`. Wholly absent sessions still appear in coverage when
explicit date bounds are provided. Closed exchange dates remain closed rather
than being labelled inactive opportunities.

## Planned target R

`target_r` is a decimal **string** from 0.25 through 10. It expresses planned
price reward as a multiple of entry-to-stop risk, not account capital or a
promised net payoff. Target selection is a research input, not an optimiser.

For a 100.20 signal close and 99 stop, 3R gives a reference target of 103.80.
A modeled fill at 100.30 recomputes the target to 104.20. Costs/flattening/gaps
can change net payoff. The detector and simulator consume the same validated
policy, rather than inferring a multiple from rounded prices or changing only
a chart label. Stops still win an ambiguous one-minute stop/target candle;
missing entry/holding bars remain unfilled/unresolved. The idealised immediate
full-fill and unrounded-price assumptions remain disclosed and uncalibrated.

Changing later candles cannot alter an earlier signal or admission snapshot;
new signals after those changes may of course differ. Tests compare the causal
prefix, not an invalid assumption that the entire future signal list is fixed.
A synthetic 3R example can stop out where 2R takes profit; it is a software
counterexample to assuming higher R means better performance, not evidence for
choosing a real trading parameter.

## Contracts and compatibility

ResearchSettings v1 remains an exact eight-field risk/cost snapshot with implicit
all weekdays and 2R. Its stored rows, hashes and Reference identity do not change.
ResearchSettings v2 requires those fields **plus** explicit target_r/weekdays.
The UI now projects v1/v2 meaning into an editable v3 draft with explicit
entry/concurrency caps (see entry-capacity.md). Only a deliberate save
appends a v3 version; no prior row/run is migrated or reinterpreted. Old versions
remain inspectable and usable.

The existing preset SQLite schema needs no migration. ORB Reference is protected
at `orb-reference`/v1; ORB MWF is protected at `orb-mwf`/v1. Duplicate either to
create a custom variant. Optimistic saved-version checks and immutable run
snapshots continue to apply. Concurrent paper profiles are not enabled; their
future account-wide duplicate-signal/risk coordination must not rely solely on
these different research signal IDs.

Baseline evaluations keep the v5 contract. Non-reference policies use
`orb-evaluation-v6`, adding only the explicit entry_policy configuration and
variant strategy/model identity. Bundle/profile wrappers retain their base
schema. Imports accept supported v4/v5/v6 evidence, validate policy fields against
saved settings, match original source identity and recompute current results.
Old report metrics are never upgraded into trusted results by relabelling them.
The fixed reference rule block remains visible separately from applied variant
rules. CLI/API/UI use the same policy/settings contract.

## Local workflow

In Applied settings, select ORB MWF to inspect it, duplicate it, choose weekdays
and a planned R multiple, save, then apply the exact saved version to a new run.
A reference comparison should isolate weekday choice first with the same costs
and all other settings, rather than changing weekdays and R together and
attributing the result to either one.

```bash
python -m orb.presets --db results/dashboard-presets.sqlite list
python -m orb.presets --db results/dashboard-presets.sqlite create \
  --name 'MWF research' --settings fixtures/research_settings_mwf.json
python -m orb.presets --db results/dashboard-presets.sqlite run \
  --id orb-mwf --version 1 --csv fixtures/orb_execution_sample.csv \
  --output results/mwf-profile.json
```

Repository prices are synthetic. Zero costs in the illustrative MWF preset match
the fixed diagnostic reference; they are not calibrated realistic costs. Broad
comparisons need permitted multi-period data, cost sensitivity, paired session
outcomes, chronological holdouts and a log of every tried variant. No performance
ranking or automatic strategy switching is introduced here.

Entry/concurrency controls and durable offline counters now exist; see
[capacity semantics](entry-capacity.md). Next: durable research jobs/comparisons
and remaining paper partial-fill/recovery integration. Range/entry timing, alternative stop policies, news and
paper controls are not made operational by this increment.
