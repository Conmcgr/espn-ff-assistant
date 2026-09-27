# Fantasy Football Agent — Architecture and Roadmap

_Updated September 26, 2026 after the ESPN feasibility scan and first Supabase
load._

## Product direction

The product helps one user make better waiver, trade, and start/sit decisions by
understanding the user's preferences and the specific managers in each league.
It optimizes for expected fantasy outcomes, uses preferences as soft constraints,
and treats `NO_ACTION` as a valid result. League A is first; League B uses the
same architecture later.

The implementation is CLI-first. SMS remains the intended delivery adapter, but
it follows a working recommendation loop. Recommendations remain advisory; no
ESPN write operations are permitted.

## Feasibility boundary

The ESPN scan covered 2013–2026. Weekly roster payloads were returned for every
scanned season. Structured `transactions` data was absent from 2013–2017 and
present from 2018 onward. The archive produced 5,772 unique transactions from
2018–2026, including waivers, FAAB bids, free-agent moves, trades, lineup
changes, and drafts. Activity is available as optional current-season enrichment
but is not a historical dependency.

Use 2018 onward as the primary transaction-modeling window. Older seasons can
provide broader roster, matchup, draft, settings, and standings context, with
explicit gaps and uncertain manager identity joins.

## Target architecture

```text
ESPN + football-data providers
              ↓
       read-only collectors
              ↓
   raw private archive + manifest
              ↓
       normalizers / upserts
              ↓
      managed PostgreSQL state
              ↓
  diffs + deterministic opportunity gate
              ↓
       opportunity queue
              ↓
    one reasoning orchestrator
              ↓
 recommendation or NO_ACTION
              ↓
      CLI now, SMS later
              ↓
 feedback + outcomes + feature updates
```

Deterministic software owns synchronization, persistence, scoring, legality,
diffing, filtering, scheduling, and obvious rankings. The reasoning orchestrator
uses tools for ambiguous waiver, trade, and lineup decisions and must return
structured evidence, confidence, uncertainty, and whether to notify.

## Data model boundary

The normalized system of record is managed PostgreSQL. Large raw JSON payloads
remain in private file/object storage and are referenced by run ID, hash, season,
view, and scoring period.

Initial relational entities:

`users`, `leagues`, `league_seasons`, `sync_runs`, `raw_payloads`, `managers`,
`manager_season_identities`, `teams`, `league_settings`, `scoring_periods`,
`roster_snapshots`, `roster_entries`, `matchups`, `draft_picks`, `transactions`,
`transaction_items`, `manager_features`, `opportunities`, `recommendations`,
`recommendation_evidence`, `feedback`, and `recommendation_outcomes`.

Use internal IDs plus ESPN provider IDs. Never use a season-specific team ID as a
permanent manager identity. Store identity confidence and source provenance.

## Phased roadmap

### Phase 0 — Experiment closeout

Make the scan reproducible, private, resumable, and auditable. Complete manual
verification for representative draft, matchup, roster, add/drop, FAAB, and trade
records. Freeze the 2018–2026 transaction window as the first modeling scope.

### Phase 1 — Persistence foundation

Apply the initial PostgreSQL migration, add managed-DB configuration, create sync
run and raw-payload reference tables, and introduce repository boundaries. Prove
idempotent upserts with synthetic fixtures before loading League A.

### Phase 2 — Normalized League A state

Load 2026 first, then 2018–2025: settings, identities, teams, scoring periods,
rosters, matchups, drafts, transactions, items, and ownership intervals. Load
older seasons as partial context without inventing transaction records.

### Phase 3 — Manager analytics

Compute measured waiver, FAAB, trade, roster-churn, lineup, draft, and holding
period features with sample sizes, observation windows, and confidence. Avoid
psychological labels.

### Phase 4 — External football data

Add provider-neutral adapters for projections, usage, injuries, schedules, depth
charts, and news. Persist source and retrieval timestamps; do not build proprietary
projections first.

### Phase 5 — Opportunity engine

Detect meaningful state changes, rank candidate waiver/trade/lineup opportunities,
apply urgency and notification gates, deduplicate opportunities, and expire stale
ones. Most events should stop here without invoking the reasoning layer.

### Phase 6 — Waiver vertical slice

Build deterministic add/drop and FAAB candidates, use the single orchestrator for
ambiguous evidence synthesis, expose CLI scan/explain/feedback commands, and store
every recommendation including suppressed ones.

### Phase 7 — Trades and start/sit

Separate user-side trade quality from counterparty acceptability, then add lineup
legality, projections, injuries, and close-decision analysis.

### Phase 8 — Evaluation and runtime

Backtest point-in-time recommendations against generic baselines, add scheduled
syncs and event windows, then add SMS as a transport adapter. Add League B only
after League A has a measured recommendation loop.

## Guardrails

- No automatic ESPN actions.
- No multi-agent architecture in V1.
- No future-data leakage in backtests.
- No raw credentials in logs, database rows, or tracked files.
- No activity-feed dependency for historical modeling.
- No unsupported psychological inference from sparse behavior.
- No production schema coupled directly to ESPN response objects.

## Current implementation checkpoint

_Updated September 27, 2026._

- **Loaded data:** all 14 seasons in Supabase, including 1,333 matchups.
- **Manager features:** attributed by team; FAAB reconciles with SQL.
- **Deterministic recommendations:** a start/sit and waiver loop (Phases 5–6
  plus lineup) runs from the CLI.
  - Inputs: ESPN projections, a live free-agent pool, and the pro schedule.
  - Every recommendation, including `NO_ACTION`, is persisted with evidence,
    feedback, and outcomes.
- **Backtests:** point-in-time backtests calibrate the lineup gate and the
  FAAB rule.

See `docs/recommendation-engine-handoff.md` §12 for status, findings, and
next steps.
