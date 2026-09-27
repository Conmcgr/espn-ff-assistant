# Recommendation Engine — Implementation Handoff

_Written September 27, 2026 (2026 season, after week 3). Covers roadmap item
"start/sit and waiver recommendations" — Phases 5–6 of
`docs/architecture-and-roadmap.md` plus the start/sit half of Phase 7._

Read `AGENTS.md` and `fantasy_football_agent_handoff_detailed.md` §3–4, §7–8,
§10, §15–16 first. This document tells you what to build and in what order.
It does not repeat the product thesis.

---

## 0. Goal and non-goals

**Goal:** a deterministic, CLI-first loop that, for the user's League A team,
produces:

1. a **lineup recommendation** (start/sit) for the upcoming scoring period, and
2. **waiver recommendations** (add X / drop Y / bid $N) before each waiver run,

or `NO_ACTION`. Every result gets persisted, including suppressed ones and
`NO_ACTION`, and outcomes are scored once the week finishes.

**Non-goals for this plan:**

- LLM reasoning orchestrator. It comes after the deterministic loop has
  measured outcomes, and is sketched only in §9.
- Trades.
- SMS.
- Proprietary projections. ESPN projections are consumed as a provider
  signal only.
- Any ESPN write action.

### Required scope change

**Done in R0.** `AGENTS.md` previously listed "projections" and "LLM
workflows" as out of scope. It now says:

- **Allowed:** ingesting provider projections (ESPN first), deterministic
  lineup/waiver recommendation logic, and persisting recommendations,
  feedback, and outcomes.
- **Still out of scope:** building our own projection model, LLM workflows,
  SMS, dashboards, deployment, and ESPN writes.

---

## 1. What exists today (verified September 27, 2026)

| Asset | State |
|---|---|
| Seasons loaded | 2013–2026 (14), 2026 through week 3 |
| Weekly roster snapshots / entries | 2,536 / 38,346 |
| Matchups | 1,333, all seasons |
| Transactions / items | 5,772 / 9,914 (2018+) |
| Ownership intervals | 4,075 |
| Manager features | 544 rows as of 2026 wk 3, 17 stats × 32 managers |
| Repository | `src/espn_ff_assistant/repository.py`: `roster_at`, `matchups`, `standings_as_of`, `transactions`, `manager_features`, `players_by_ids` |

### League A settings (from 2026 `mSettings`)

- Scoring: `H2H_POINTS`.
- Lineup slot counts:
  - QB(0) ×1
  - RB(2) ×2
  - WR(4) ×2
  - TE(6) ×1
  - FLEX(23) ×1
  - D/ST(16) ×1
  - K(17) ×1
  - Bench(20) ×6
  - IR(21) ×1
- Acquisition:
  - `WAIVERS_TRADITIONAL` with FAAB (`isUsingAcquisitionBudget: true`), budget $200, minimum bid $0.
  - Waivers process **Wednesday and Saturday at 11:00** (league timezone to be confirmed), 24-hour waiver period, waiver order resets.
- Always read these from settings at runtime. Never hardcode them.

### Signals already in archived `mRoster` payloads (not yet normalized)

Per player, under `playerPoolEntry.player`:

- `stats[]` entries keyed by `(seasonId, scoringPeriodId, statSourceId, statSplitTypeId)`:
  - `statSourceId`: 0 = actual, 1 = projected.
  - `statSplitTypeId`: 1 = single week, 0 = full season.
  - `statSplitTypeId` 2 appears on season-level projections and is **probably** rest-of-season. Verify this (§10).
  - A week-N snapshot contains the week-N projection, the week-(N−1) projection and actual, and season totals.
- `injuryStatus` (ACTIVE / QUESTIONABLE / DOUBTFUL / OUT / IR / SUSPENDED …), `injured`.
- `eligibleSlots` (list of lineup slot IDs), `defaultPositionId`, `proTeamId`.
- `ownership.{percentOwned, percentStarted, percentChange, averageDraftPosition}`.
- Entry-level: `lineupSlotId`, `playerPoolEntry.lineupLocked`, `rosterLocked`, `status`.

### Not yet collected

- **Free-agent / waiver pool.** Required for waivers.
- **NFL pro-team schedule.** Required for bye weeks and game lock times.
- **Remaining FAAB per team.** Check `mTeam` for a field like `transactionCounter.acquisitionBudgetSpent`; if absent, derive it from executed claims.

---

## 2. Step R0 — Fix manager features the engine depends on

FAAB suggestions and competitor modelling rely on waiver features. Those are
wrong today. Fix them first, in their own PR.

### Verified defects

1. **Executed waiver claims are never attributed.** All 796 `EXECUTED` waiver
   claims ($10,203 total FAAB) have the same `provider_member_id`, which
   matches no manager. It looks like a league/system actor. Failed claims
   carry the real member ID only about 42% of the time (381 of 897 match).
   - Effect: `waiver_claims_total = 0` and FAAB stats show `insufficient` for
     every manager, and `waiver_fail_rate = 1` wherever n > 0.
   - **Fix:** attribute waiver and free-agent transactions by
     `(league_season_id, provider_team_id)` → `team_owners`. Never use
     `provider_member_id` for these. Add a `validate_db.py` check that
     reports the share of claims whose member ID resolves to a manager.
2. **Fail rate counts non-failures.** `PENDING` (301) and `CANCELED` (158)
   are counted as failures.
   - **Fix:** the denominator is `EXECUTED` plus `FAILED_*` only.
   - Also emit `waiver_lost_bid_rate` using the failure reasons that mean
     "outbid" (probably `FAILED_INVALIDPLAYERSOURCE`; verify, §10).
     Budget and roster failures are a different signal.
3. **`adds_per_week` sample size is weeks, not observations.** Managers with
   0 adds show `n=136 [high]`.
   - **Fix:** confidence uses the number of adds. Store the week count in a
     separate `weeks_observed` feature.
4. **Co-owners get identical copies of team-derived stats.** Two members on
   the same team show byte-identical adds, drops, and holding periods.
   - **Fix:** add team-season-level features for FAAB and churn. Mark
     manager-level rows derived from shared teams with `shared_team=true`
     (new column), or aggregate by team.
   - For competitor modelling, the **team** is the bidding unit, so the
     engine should read team-level features.

### New FAAB features to add (needed by §5)

All of these are point-in-time, 2018+, executed claims only:

- `faab_bid_pct_of_remaining`: bid ÷ budget remaining at bid time. Report median and p75.
- `faab_bid_by_position`: median winning bid per position.
- `faab_early_season_share`: share of spend in weeks 1–4.
- `faab_remaining`: current budget left. Implemented as
  `Repository.faab_remaining()` (budget minus executed spend through the
  as-of week), not as a manager feature, because it is team state.

**R0 status (done):**
- Migration 008 adds `manager_features.shared_team`.
- Migration 009 backfills the sentinel team IDs.
- Features are now version 2, and `Repository.manager_features()` returns
  the newest version by default.
- Team-attributed FAAB reconciles exactly with SQL: $10,203 across 796
  executed claims.

### Acceptance

- `manager_report.py` shows non-zero executed claims and FAAB for active managers.
- League-wide sum of executed-claim FAAB in features equals the SQL total.
- Tests cover system-member attribution and fail-rate denominators.
- New migration if a column is added. Old migrations stay untouched.

---

## 3. Step R1 — Current-week ingest (players, projections, availability)

### Migration `010_player_week_state.sql`

```sql
-- One row per player × period × source × split, per sync run (projections move during the week).
CREATE TABLE player_week_stats (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  league_season_id UUID NOT NULL REFERENCES league_seasons(id),
  provider_player_id BIGINT NOT NULL,
  scoring_period INTEGER NOT NULL,          -- 0 = season-level
  stat_source TEXT NOT NULL,                -- 'actual' | 'projected'
  stat_split TEXT NOT NULL,                 -- 'week' | 'season' | 'ros' (verify id 2)
  applied_total NUMERIC,
  sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
  retrieved_at TIMESTAMPTZ NOT NULL,
  raw_payload_uri TEXT,
  UNIQUE (league_season_id, provider_player_id, scoring_period, stat_source, stat_split, sync_run_id)
);

CREATE TABLE player_status_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  league_season_id UUID NOT NULL REFERENCES league_seasons(id),
  provider_player_id BIGINT NOT NULL,
  scoring_period INTEGER NOT NULL,
  injury_status TEXT,
  eligible_slots INTEGER[],
  pro_team_id INTEGER,
  percent_owned NUMERIC, percent_started NUMERIC, percent_change NUMERIC,
  availability TEXT,                        -- 'rostered' | 'free_agent' | 'waivers'
  on_provider_team_id BIGINT,
  waiver_clear_at TIMESTAMPTZ,
  lineup_locked BOOLEAN,
  sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
  retrieved_at TIMESTAMPTZ NOT NULL,
  raw_payload_uri TEXT,
  UNIQUE (league_season_id, provider_player_id, scoring_period, sync_run_id)
);

CREATE TABLE pro_team_games (
  season INTEGER NOT NULL, scoring_period INTEGER NOT NULL,
  pro_team_id INTEGER NOT NULL, opponent_pro_team_id INTEGER,
  kickoff_at TIMESTAMPTZ, is_bye BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (season, scoring_period, pro_team_id)
);
```

Enable RLS and revoke Data API access on every new table, matching the
existing tables.

### Normalizers (pure, no I/O)

In `src/espn_ff_assistant/normalize/players.py`:

- `week_stats(season, payload) -> list[row]`: flattens `stats[]` from roster entries and pool entries.
- `status_rows(season, period, payload, availability) -> list[row]`.
- `pro_schedule(payload) -> list[row]`.

### Collector: `scripts/sync_current.py`

Read-only. Archives raw responses under `data/raw/<run>/` exactly like the scan does, then loads.

1. Fetch `mSettings`, `mTeam`, `mRoster` (current period), `mBoxscore` (current period), and `mTransactions2` (current period). Reuse the `load_run.py` normalizers for rosters, transactions, and matchups.
2. Fetch the player pool: `view=kona_player_info` for the current period with an `X-Fantasy-Filter` header. `ESPNClient.fetch` already supports this through `fantasy_filter=`. Starting filter (**unverified**, check against a live response):
   ```json
   {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},
    "filterSlotIds":{"value":[0,2,4,6,16,17,23]},
    "sortPercOwned":{"sortPriority":1,"sortAsc":false},
    "limit":300}}
   ```
   Store `availability` from `status` and the waiver clear date (field name to be verified).
3. Fetch the pro schedule: `view=proTeamSchedules_wl` at the season (not league) endpoint. **Unverified.** If unavailable, derive byes from player `proTeamId` plus the absence of weekly projections, and report it as a degraded state.
4. Record distinct states for missing, empty, partial, and failed responses, per `AGENTS.md`.

### Backfill

Run the week-stats normalizer over all 217 archived `mRoster` payloads. This
gives historical weekly projections and actuals for rostered players, which
the lineup backtest (§7) needs. Historical free-agent pools cannot be
recovered; record that as a known gap.

### Repository additions

All take an explicit point in time:

- `projections(league_id, season, period, player_ids, as_of_ts) -> dict[pid, Projection]` (latest retrieval at or before `as_of_ts`)
- `player_status(league_id, season, period, player_ids, as_of_ts)`
- `available_players(league_id, season, period, as_of_ts, positions=None, limit=None)`
- `pro_games(season, period)`
- `faab_remaining(league_id, season, as_of_week) -> dict[team_id, int]`
- `user_team(league_id, season) -> provider_team_id` (see §6 identity)

### Acceptance

- `sync_current.py` can be re-run safely: two runs in a row produce two sync runs and no duplicate rows within a run.
- The loaded free-agent pool excludes every rostered player.
- 2026 week-4 projections exist for all rostered players.
- Bye teams are identified for week 4.

---

## 4. Step R2 — Lineup optimizer (start/sit)

Module: `src/espn_ff_assistant/lineup.py`. Pure functions with no DB access.
The CLI assembles the inputs through the repository.

### Inputs

```python
@dataclass(frozen=True)
class LineupPlayer:
    provider_player_id: int
    name: str
    eligible_slots: frozenset[int]
    projection: float | None      # week projection, provider units
    injury_status: str | None
    on_bye: bool
    locked: bool                  # game started; slot is fixed
    current_slot: int
```

The slot configuration comes from `lineupSlotCounts`. Supported slot IDs to
start: 0, 2, 4, 6, 16, 17, 23 (FLEX = RB/WR/TE), 20 (bench), 21 (IR).
Raise a clear error on any other slot with a non-zero count, so a future
superflex or OP league fails loudly instead of silently.

### Effective projection

| Condition | Effective value | Flag |
|---|---|---|
| OUT, IR, SUSPENDED, or on bye | 0 and not startable | `ineligible_reason` |
| DOUBTFUL | projection × 0.25 | `injury_risk` |
| QUESTIONABLE | projection × 0.85 | `injury_risk` |
| Projection missing | 0 | `no_projection` |

The multipliers are named constants in one place, versioned with
`ENGINE_VERSION`. They are placeholders to calibrate in §7. Do not tune them
by feel.

### Algorithm

This is maximum-weight assignment of players to starting slots.

- Locked players stay in their current slot and are removed from the problem.
- For League A's slot set (dedicated slots plus one RB/WR/TE flex), greedy is
  optimal: fill each dedicated slot with its best eligible players by
  effective projection, then fill FLEX with the best remaining RB/WR/TE.
- Implement it generally anyway: brute force over which eligible players
  fill flex-type slots. That is tiny at 16-man rosters.
- **Test:** property tests against exhaustive search on random synthetic
  rosters. Do not add scipy.

### Output

```python
@dataclass
class LineupRecommendation:
    optimal: dict[int, list[int]]          # slot -> player ids
    moves: list[tuple[int, int, int]]      # (player, from_slot, to_slot)
    current_points: float                  # effective, current lineup
    optimal_points: float
    gain: float
    ineligible_starters: list[int]         # in active slot but OUT/bye/IR
    close_calls: list[CloseCall]           # |Δ| < CLOSE_MARGIN between a starter and best bench alternative
    flags: dict[int, list[str]]
```

### Notification gate (deterministic)

| Decision | Condition |
|---|---|
| `urgent` | Any ineligible starter where a startable replacement exists |
| `notify` | `gain ≥ LINEUP_NOTIFY_MIN_GAIN` (start at 2.0 points) |
| `info` | Only close calls, or gain below the threshold |
| `NO_ACTION` | Current lineup is optimal |

Rules on top of that:

- Close calls never notify on their own. They are the future hand-off point
  to the reasoning layer (§9).
- Near kickoff, only unlocked players can be moved. Recompute after each
  game window.

### Acceptance

- Unit tests cover: bye in a starting slot, OUT starter, QUESTIONABLE discount, FLEX choice, locked players, a user already optimal (`NO_ACTION`), and an unsupported-slot error.
- The optimizer matches brute force on 500 random rosters.

---

## 5. Step R3 — Waiver engine

Module: `src/espn_ff_assistant/waivers.py`. Pure core, with inputs assembled
by the CLI.

### 5.1 Candidate generation

- Start from the available pool (free agents plus waivers) at `as_of_ts`.
- Drop players with no projection and `percent_owned < 1`.
- Cap at the top 40 by rest-of-season projection, plus the top 10 by next-week projection per position.

### 5.2 Value of an add/drop pair

Roster value comes from the lineup optimizer:

```
V(roster, horizon) = Σ over weeks w in horizon of optimal_points(roster, week w)
```

**Horizon:** `next_week` = 1 week; `ros` = remaining regular-season weeks.

For weekly ROS values, use the rest-of-season total ÷ remaining games when
week-level projections are unavailable. Byes are zeroed via `pro_team_games`.

For each candidate X and each droppable player Y on the user's roster:

```
Δ_next(X, Y) = V(R − Y + X, next_week) − V(R, next_week)
Δ_ros(X, Y)  = V(R − Y + X, ros)       − V(R, ros)
```

**Best drop for X:** `argmax_Y Δ_ros`.

**Not droppable:** players in the IR slot, locked players, and players the
user marked as a protected stash (§6).

### 5.3 Preference priors (weak, from handoff §3)

- **Streams defenses:** score D/ST and K candidates on `Δ_next` only. They
  are one-week rentals.
- **Stashes upside and injured players:** add a bench-value term, so the
  engine doesn't recommend dropping an injured player whose ROS projection
  is high. Also allow "stash" adds (high ROS, low `Δ_next`) when a bench spot
  would otherwise go to a low-ROS player.
- **Upside, but not at the cost of a clearly worse move:** priors only break
  ties within `PREFERENCE_TIE_BAND` of the top option. They never override
  a clearly larger Δ.

Priors live in `user_preferences` (§6), each with a `weight` and a
`source` (`onboarding` or `learned`).

### 5.4 FAAB suggestion

Only for players on waivers. Free agents cost $0; the engine says "add now"
instead of bidding.

Inputs:

- `comparables`: executed claims (2018+, prior seasons plus the current
  season up to `as_of`) in the same position × player tier.
  - Tier = next-week projection rank among available players that week.
  - If historical tiers can't be reconstructed, fall back to position × week
    bucket.
- `competitors`: other teams with positional need at X's position.
  - Need = a starter at that position is ineligible next week, or the
    team's worst starter projects below X.
  - Weight each competitor by its `faab_bid_pct_of_remaining` and
    `faab_remaining` (team-level features from §2).

Rule (v1, transparent):

```
base   = median(comparable winning bids)
demand = 1 + 0.15 × (number of competitors with need and faab_remaining ≥ base)
bid    = clamp(round(base × demand), min_bid, min(user_faab_remaining, value_cap(Δ_ros)))
```

- Output a point value plus a range (p25–p75 of the comparables).
- Confidence comes from the comparable sample size, using the existing
  `_confidence` thresholds.
- `value_cap` keeps a marginal player from getting a large bid. Start with
  a linear $/ROS-point factor calibrated from 2018–2025 claims, and state it
  in the evidence.

### 5.5 Ranking and gate

- Rank candidates by `Δ_ros`, with `Δ_next` as the tie-break. D/ST and K use `Δ_next`.
- **Notify** if the top candidate has `Δ_ros ≥ WAIVER_NOTIFY_MIN_ROS` or
  `Δ_next ≥ WAIVER_NOTIFY_MIN_NEXT`. Include at most 3 recommendations.
  Everything else is stored as suppressed.
- **`NO_ACTION`** if no pair has a positive Δ above noise.
- **Timing:** a pre-waiver scan the evening before each process day (Tuesday
  and Friday) and a post-waiver scan after processing for newly dropped
  players. These are CLI invocations for now; scheduling is Phase 8.

### Acceptance

- Unit tests on synthetic rosters:
  - an injured starter's replacement is recommended;
  - a better D/ST is recommended as a stream;
  - a high-ROS injured stash is not recommended as the drop;
  - the bid is clamped to the user's remaining budget;
  - a free agent gets a $0 "add now";
  - an already-strong roster gets `NO_ACTION`.
- FAAB comparables query is point-in-time: a test shows a future claim is excluded.

---

## 6. Step R4 — Recommendation persistence, user identity, preferences

### Migration `011_recommendations.sql`

```sql
CREATE TABLE user_teams (             -- which team is "me" per season; no hardcoding
  user_id UUID NOT NULL REFERENCES users(id),
  league_season_id UUID NOT NULL REFERENCES league_seasons(id),
  provider_team_id BIGINT NOT NULL,
  PRIMARY KEY (user_id, league_season_id)
);

CREATE TABLE user_preferences (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  league_id UUID REFERENCES leagues(id),
  key TEXT NOT NULL,                  -- 'streams_dst', 'stash_injured', 'protected_stash:<pid>', ...
  value JSONB NOT NULL,
  weight NUMERIC NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL,               -- 'onboarding' | 'learned' | 'explicit'
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, league_id, key)
);

CREATE TABLE recommendations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  league_season_id UUID NOT NULL REFERENCES league_seasons(id),
  kind TEXT NOT NULL,                 -- 'lineup' | 'waiver'
  scoring_period INTEGER NOT NULL,
  as_of_ts TIMESTAMPTZ NOT NULL,
  sync_run_id UUID REFERENCES sync_runs(id),
  decision TEXT NOT NULL,             -- 'urgent' | 'notify' | 'info' | 'suppressed' | 'no_action'
  summary TEXT NOT NULL,
  payload JSONB NOT NULL,             -- structured rec (moves / add-drop-bid)
  alternatives JSONB,
  confidence TEXT,
  engine_version TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recommendation_evidence (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recommendation_id UUID NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                 -- 'projection' | 'injury' | 'manager_feature' | 'faab_comparables' | ...
  key TEXT NOT NULL, value JSONB NOT NULL, source TEXT
);

CREATE TABLE recommendation_feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recommendation_id UUID NOT NULL REFERENCES recommendations(id),
  action TEXT NOT NULL,               -- 'accepted' | 'rejected' | 'modified' | 'ignored' | 'impossible'
  reason TEXT,                        -- 'too_much_faab' | 'dont_believe_player' | 'prefer_current' | 'other'
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recommendation_outcomes (
  recommendation_id UUID PRIMARY KEY REFERENCES recommendations(id),
  observed_action TEXT,               -- inferred from next roster snapshot / transactions
  recommended_points NUMERIC, actual_points NUMERIC, baseline_points NUMERIC,
  evaluated_through_period INTEGER,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Enable RLS and revoke Data API access on all of these.

Repository methods:

- `save_recommendation(rec, evidence) -> id`
- `recommendation(id)`
- `recommendations(user, season, period, kind=None)`
- `save_feedback(...)`
- `save_outcome(...)`
- `user_team(...)`
- `preferences(user, league)`
- `set_preference(...)`

### Rules

- Persist **every** run: `NO_ACTION` gets a row, and suppressed candidates get rows.
- Ignored does not mean disliked. `ignored` feedback never changes preferences.
- Only explicit feedback or repeated rejections with the same reason
  (≥ 3 times) may adjust a `learned` preference weight. That is a later
  step; v1 only records it.
- Identity: bootstrap `users` and `user_teams` with a one-off CLI
  (`recommend.py whoami --team <id>`). Never commit member IDs or team
  mappings to tracked files.

---

## 7. Step R5 — Outcomes and backtest

### Outcome scoring

Run with `scripts/score_outcomes.py --season --week` after a week completes.

- **Observed action:** compare the next roster snapshot and the
  transactions against the recommendation, and set `observed_action`.
- **Lineup:** compute three totals and store them:
  - actual points of the recommended lineup;
  - actual points of the user's actual lineup;
  - points of the ESPN-projection-greedy baseline.

  If the engine already is that greedy baseline, record the ESPN default
  lineup (the user's lineup unchanged) as the baseline instead.
- **Waiver:** the added player's actual points over the next 1 and 3 weeks,
  minus the dropped player's, counting only started weeks (optimizer view).

### Backtest (lineup only, historical)

- Use the §3 backfill of archived weekly projections and actuals for
  rostered players, 2018–2025.
- For every team-week, compare the optimizer lineup (from the week's
  projections) with the lineup that was actually set. Metric: realized
  points gained.
- Use the results to calibrate the injury multipliers and
  `LINEUP_NOTIFY_MIN_GAIN`. Record the chosen values and the backtest
  window in the commit message.
- **Leakage guard:** the backtest reads only rows whose `scoring_period ≤ w`,
  and only the projection that was archived for week w.

### Waiver backtest

This is limited, because historical free-agent pools can't be recovered.
Evaluate only the FAAB rule: for each historical executed claim, would the
suggested bid have won? Compare against the runner-up bid where failed
claims on the same player exist. Report the hit rate and overpay.

### Deliverable

A private report under `data/derived/recommendation-backtest.local.md`. It is
not committed.

---

## 8. Step R6 — CLI

`scripts/recommend.py` is a thin entry point. All logic lives in `src/`.

```
recommend.py sync                                   # runs sync_current
recommend.py lineup   --season 2026 --week 4 [--save]
recommend.py waivers  --season 2026 --week 4 [--save]
recommend.py scan     --season 2026 --week 4 --save # lineup + waivers, applies gates
recommend.py explain  <recommendation_id>           # prints evidence
recommend.py feedback <recommendation_id> --action rejected --reason too_much_faab [--note ...]
recommend.py whoami   --team <provider_team_id>     # one-time identity bootstrap
```

Output format follows handoff §15:

- decision line;
- the moves;
- 2–4 "why" bullets taken from the evidence, with deterministic templates
  and no LLM;
- confidence;
- the FAAB range where relevant.

---

## 9. Later (not in this plan): reasoning orchestrator hook

The deterministic engine leaves two explicit seams:

- lineup `close_calls`;
- waiver candidates whose ranking flips between `Δ_next` and `Δ_ros`, or
  whose projection moved sharply on news.

These get recorded with `decision='info'` and a `needs_review` flag in the
payload. A single tool-using orchestrator can consume them after R5 shows
measured outcomes. Do not build it in this plan.

---

## 10. Open questions to resolve during implementation

Verify each against a live or archived response, then record the answer in
this file.

1. ~~`statSplitTypeId = 2`.~~ **Resolved (R1):** it is ESPN's *current*
   full-season projection, i.e. actual to date plus the rest-of-season
   projection. Rest-of-season = split-2 projected − season actual total.
   That gives about 13.7 per-week projections for rostered players with 14
   games left. Split 0 (source 1) is the preseason projection.
2. ~~`kona_player_info` filter.~~ **Resolved (R1):** the filter in §3 works
   (300 players, `status` of `FREEAGENT` or `WAIVERS`). The waiver clear
   time is `waiverProcessDate` (ms) on the pool entry.
3. ~~`proTeamSchedules_wl`.~~ **Resolved (R1):** it works at the season
   (game-level) endpoint. It returns `settings.proTeams[]` with `byeWeek`
   and `proGamesByScoringPeriod` (kickoff `date` in ms). ID 0 is the
   free-agent pseudo-team.
4. ~~Remaining FAAB.~~ **Resolved (R1):**
   `mTeam.teams[].transactionCounter.acquisitionBudgetSpent`. The derived
   `Repository.faab_remaining()` matches it for all 12 teams in 2026.
5. ~~`FAILED_INVALIDPLAYERSOURCE` meaning.~~ **Resolved (R0):** it means
   outbid. 307 of 314 have an executed claim on the same player by another
   team within the same waiver run, always with a bid ≥ the failed bid.
   `FAILED_MATCHUPACQUISITIONLIMIT` claims (30) also always have a same-run
   winner, but they are not counted as lost bids. Failed claims are
   runner-up bids, which the FAAB backtest can use.
6. ~~System member ID on executed claims.~~ **Resolved (R0):** one ID is
   used on every executed claim from 2018 to 2026 and matches no manager.
   Claims are attributed by team. Also, 2018 had 137 transactions with the
   sentinel `teamId = -2147483648`. The normalizer now derives the acting
   team from the ADD item, and migration 009 backfilled the stored rows.
7. ~~League timezone.~~ **Not needed:** each waiver player's
   `waiverProcessDate` gives the exact run time (e.g. Wednesday 03:00 ET).
   It is stored as `player_status_snapshots.waiver_clear_at`.
8. **New finding (R5): backfilled status is not point-in-time.** In
   historical `mRoster` payloads, `injuryStatus` and `ownership` never
   change within a season for any player (2018–2025). They are
   end-of-season values. Weekly projections and actuals are per-period and
   safe to use.
   - Consequences: the lineup backtest ignores injury status, so the injury
     multipliers are **not** calibrated.
   - FAAB comparables use ownership only from live `-current` syncs
     (`LIVE_RUN_SUFFIX`).
   - Calibrating the injury multipliers needs a season of live syncs.

---

## 11. PR sequence and definition of done

| PR | Contents | Done when |
|---|---|---|
| 1 | `AGENTS.md` scope update; R0 feature fixes (+ migration if a column is added); validation check for attribution | Executed-claim FAAB reconciles; tests green |
| 2 | Migration 010, player normalizers, `sync_current.py`, historical week-stats backfill, repository reads | Week-4 projections, pool, and byes loaded; rerun-safe |
| 3 | `lineup.py` + tests (property vs brute force) + `recommend.py lineup` | Correct lineup for week 4; `NO_ACTION` when optimal |
| 4 | Migration 011, persistence, `whoami`, `explain`, `feedback` | Every run persisted, including `NO_ACTION` |
| 5 | `waivers.py` (candidates, Δ values, priors, FAAB rule) + `recommend.py waivers` / `scan` | Pre-waiver scan produces ≤ 3 recs or `NO_ACTION` with evidence |
| 6 | `score_outcomes.py`, lineup backtest, FAAB backtest, calibrated constants | Private backtest report; constants set from data |

Every PR must meet these, per `AGENTS.md`:

- `uv run pytest` and `uv run ruff check .` pass.
- All SQL lives in `repository.py`.
- New tables have RLS enabled.
- Fixtures are synthetic.
- Credentials and member IDs don't appear in logs or tracked files.
- The code stays read-only against ESPN.

---

## 12. Implementation status (September 27, 2026)

All six PRs are implemented on branch `recommendation-engine`. They are live
against Supabase, and migrations 008–011 are applied.

| Step | Where | State |
|---|---|---|
| R0 feature fixes | `manager_stats.py` v2, migrations 008–009, `validate_db.py` attribution check | Done; FAAB reconciles exactly |
| R1 ingest | `sync.py`, `scripts/sync_current.py`, `loader.py` (`--only player_state`), `normalize/players.py`, migration 010 | Done; 186,665 historical stat rows backfilled; live sync takes about 50 s |
| R2 lineup | `lineup.py` (Hungarian assignment over `eligibleSlots`) | Done; matches brute force on 500 random rosters |
| R3 waivers | `waivers.py` | Done; FAAB rule set from the backtest |
| R4 persistence | migration 011, repository recommendation/preference methods, `engine.py` | Done |
| R5 outcomes and backtest | `evaluation.py`, `scripts/score_outcomes.py`, `scripts/backtest.py` | Done; report in `data/derived/recommendation-backtest.local.md` |
| R6 CLI | `scripts/recommend.py` (`sync`, `whoami`, `lineup`, `waivers`, `scan`, `explain`, `feedback`, `prefs`) | Done |

### Design changes from the plan

- **Lineup optimizer is fully general.** It uses exact assignment over each
  player's `eligibleSlots` instead of greedy. Superflex, OP, and IDP work, so
  there is no unsupported-slot error. A slot with no startable player is
  reported as `unfilled_slots`, not filled with an OUT or bye player.
- **Stream drops.** For D/ST and K streams, the drop is chosen by next-week
  gain, then rest-of-season, then the lowest rest-of-season player. That
  keeps bench depth, because depth has no value in optimal-lineup terms.
- **Recommendations must be jointly executable.** Each drop is used at most
  once, and at most one stream per position is notified. Conflicting
  options are stored as `suppressed` with reason
  `conflicts_with_higher_ranked`.
- **FAAB rule (data-driven, replaces the median × demand sketch).**
  - With no rival need: bid the 25th percentile of prior winning bids at the
    position.
  - With a rival that needs the player and has budget: bid the 75th
    percentile × (1 + 0.15 × (rivals − 1)).
  - Streams always bid at the 25th percentile.
  - Backtest, 2018–2025: 30% of claims were contested. Contested winners
    paid a median $15.50, uncontested a median $5. The 75th percentile won
    75% of contested runs (median overpay $9). The 25th percentile won 22%.
- **The loader moved** into `src/espn_ff_assistant/loader.py`.
  `scripts/load_run.py` is now a thin wrapper.

### Backtest results (lineup, 2018–2025, 1,692 team-weeks)

- The optimizer beat the lineup actually set by **+2.35 points per
  team-week** on average. Mean projected gain was +2.38, so ESPN projections
  are well calibrated for this purpose.
- `NOTIFY_MIN_GAIN = 2.0` is supported: team-weeks with projected gain ≥ 2
  realized **+8.7** on average and were positive 75% of the time. At ≥ 0.5
  the rate was 63%.

### Known gaps and next steps

1. **Injury multipliers** (0.25 for doubtful, 0.85 for questionable) are
   uncalibrated placeholders; see open question 8. Run
   `recommend.py sync` at least daily in-season so live status accumulates.
2. **Waiver notify thresholds are uncalibrated.** `NOTIFY_MIN_ROS = 10` and
   `NOTIFY_MIN_NEXT = 3` need outcome data from `score_outcomes.py`.
   Historical free-agent pools can't be recovered.
3. **Rival need uses next-week starter floors only.** Competitor FAAB
   aggression (`faab_bid_pct_of_remaining_*`) is computed but not yet used
   in bids.
4. **Bench value is zero** in optimal-lineup terms. Handcuff and injury
   insurance value is only protected through the `stash_injured`
   preference and `protected:<player_id>` preferences.
5. **Scheduling (Phase 8).** Cadence is manual for now:
   - `sync`, then `scan --save` the evening before each waiver run
     (Tuesday and Friday), and before lineups lock;
   - `score_outcomes.py` after Monday night.
6. **The reasoning-orchestrator seam** is `payload.needs_review` on lineup
   close calls (§9).
