# Fantasy Football Agent — Project Handoff

_Last updated: September 2026_

## 1. Product thesis

Build a **proactive fantasy football agent** that helps a user win while learning:

1. **how the user likes to play**, and
2. **how the specific managers in that user's leagues behave**.

The product should not just be another fantasy chatbot or rankings wrapper. Existing products such as FantasyPros already do generic start/sit, waiver, trade, projections, league sync, and AI-assisted fantasy advice well.

The differentiated idea is:

> **An agent that understands your league as a persistent social/strategic environment, understands your own play style, watches for meaningful changes, and only surfaces opportunities worth acting on.**

The product should optimize for winning first. Personal preferences act as **soft constraints and tiebreakers**, not as a reason to recommend clearly worse moves.

Example: if two bench stashes are similarly valuable, favoring the higher-upside player makes sense for a user who likes upside. If one player is materially worse, the agent should not recommend him just because he better matches the user's stated style.

---

## 2. Initial user and league context

The first version is being built for one user and two ESPN leagues.

### League A
- 12 teams
- Half-PPR
- FAAB
- Has existed since roughly 2012/2013
- Fairly stable manager pool, although some managers have come and gone
- This should be the first league supported

### League B
- 14 teams
- PPR
- Waiver priority instead of FAAB
- Almost all of the same managers as League A

The overlap in managers across two leagues is useful. It gives the system a chance to distinguish manager tendencies from league-format effects and situational behavior.

---

## 3. User play style and personalization

Known starting preferences:

- streams defenses,
- likes stashing upside players,
- likes stashing injured players when there is plausible future value,
- trades more than most managers,
- generally likes upside, but not at the cost of making a clearly worse move.

The product can collect a small amount of onboarding information like this, but onboarding preferences should be treated as **weak priors**.

The more important personalization source is actual behavior over time.

### Feedback on recommendations

Feedback should be optional and low-friction.

Useful structured reasons include:

- `too much FAAB`
- `don't believe the player`
- `prefer my current player`
- `other`

The system should also observe whether a recommendation was accepted, rejected, modified, ignored, or impossible because circumstances changed.

Important rule:

> **Ignored does not mean disliked.**

The agent should not aggressively update a preference from silence.

### Personalization objective

> **Maximize the user's chances of winning, while accounting for persistent strategic preferences when multiple reasonable options exist.**

This is different from simply imitating the user. The agent should be willing to disagree when the evidence strongly supports another move.

---

## 4. Core product capabilities

### Waivers
- identify worthwhile pickups,
- identify the best corresponding drop,
- recommend FAAB or waiver-priority strategy,
- identify stash candidates,
- react to newly dropped players,
- account for the user's roster and league environment.

### Trades
- identify players worth targeting,
- identify counterparties with plausible roster incentives,
- generate offers,
- evaluate whether a trade is good for the user,
- separately evaluate whether the other manager is likely to consider it.

This separation is important:

> **Trade quality** and **trade acceptability to this specific manager** are different problems.

The second problem is one of the major differentiators from generic fantasy tools.

### Start / sit
- recommend lineups,
- investigate close or uncertain decisions,
- account for injuries, usage, projections, matchup state, and user strategy.

Most start/sit computation should probably be deterministic. The agent is most useful when there is genuine ambiguity.

---

## 5. Main differentiator: league-specific intelligence

Generic fantasy products understand players.

This product should increasingly understand **the league**.

For each manager, the system should build a model from observable behavior such as:

- trade frequency,
- previous trade partners,
- positions acquired and traded away,
- one-for-one vs. two-for-one tendencies,
- FAAB bidding behavior,
- waiver-priority usage,
- roster churn,
- positions they stream,
- draft tendencies,
- player holding periods,
- current roster needs,
- current record and playoff situation,
- previous responses to offers from the user.

The system should avoid unsupported pseudo-psychology.

Good:

> Matt completed 7 of his 9 trades while below .500.

Bad:

> Matt panics when he is losing.

The LLM should reason over measured behavior. It should not invent behavioral profiles from sparse evidence.

### Example trade opportunity

A generic system might say:

> Player A and Player B have similar trade value.

This system should eventually be able to say:

> Matt just lost his RB2 and has excess WR depth. He has historically completed more trades when addressing an immediate positional hole, and he has accepted multi-player depth packages before. This is a better-than-usual time to explore a deal for his WR1.

That is the core product wedge.

---

## 6. Interaction model

### Recommendation-only control

The agent should **not make roster moves automatically** in the initial product.

It recommends. The user decides.

### SMS-first

The preferred interface is text messaging.

The user should be able to receive proactive messages like:

> **Waiver opportunity**  
> Add Player X, drop Player Y. X's usage jumped substantially this week and he is still available. He fits your bench better as an upside stash.  
>
> Suggested FAAB: $11  
> Confidence: Medium  
>
> Reply with a question or tell me why you are passing.

The user should also be able to trigger analysis conversationally:

- `scan waivers`
- `find me trades`
- `who should I start?`
- `what do you think of this offer?`

A polished dashboard is not required for the first version.

---

## 7. High-precision notification philosophy

The agent should optimize for **high precision rather than high engagement**.

Being silent is a valid output.

The goal is not to send fantasy content whenever anything changes. It is to surface moments that are genuinely worth the user's attention.

A first-class agent output should be:

```text
NO_ACTION
```

An event can be interesting without being actionable.

---

## 8. Event-driven behavior

The system does **not** need to be constantly running expensive agents.

The correct model is:

> **cheap collection → state change detection → relevance filtering → expensive reasoning only when warranted**

Most incoming events should update state without invoking an LLM.

### Why games should be batched

Sunday produces many simultaneous games and hundreds of player-state changes.

Running an expensive reasoning agent after every event would be expensive, noisy, context-poor, and unnecessary.

Instead, ingest data continuously or periodically, then reason over meaningful diffs after major game windows.

### Proposed trigger windows

#### After games
- roughly 30–60 minutes after Thursday Night Football,
- after the Sunday early window,
- after the Sunday late window,
- after Sunday Night Football,
- after Monday Night Football.

The actual implementation should use game status rather than hard-coded clock times.

### Waivers
- major pre-waiver analysis after the week's games are complete,
- post-waiver analysis once waivers resolve,
- daily cheap scan for newly available players and meaningful news.

### Trades and league transactions
- update league state when a trade/add/drop is detected,
- do not automatically run expensive reasoning,
- escalate only if the transaction materially changes the user's opportunity set.

### Urgent lineup events
Certain events deserve faster escalation:
- a starter is ruled out,
- a game-status change affects an imminent lineup decision,
- a strategically connected player's injury materially changes the user's decision.

---

## 9. Cost-aware pipeline

The system should be deliberately layered.

```text
ESPN + NFL stats + projections + news
                  ↓
            cheap collectors
                  ↓
          normalized state store
                  ↓
              diff engine
                  ↓
     deterministic relevance / urgency gate
                  ↓
          candidate generation
                  ↓
        only promising candidates
                  ↓
          reasoning agent
                  ↓
       recommendation threshold
            ↓             ↓
           SMS          silence
            ↓
       user feedback / action
            ↓
      persistent personalization
```

The important design principle is:

> **Events wake the data pipeline. They do not automatically wake the reasoning agent.**

Example:

```text
437 raw events
→ 52 meaningful diffs
→ 8 fantasy candidates
→ 3 relevant to the user's teams
→ 1 or 2 deep reasoning tasks
```

The exact counts will vary. The reduction pattern is what matters.

---

## 10. Agent architecture

Do **not** begin by forcing this into a multi-agent system.

The initial architecture should be:

> **one main reasoning agent with specialized tools and deterministic infrastructure underneath it**

Waiver analysis, trade analysis, start/sit, league modeling, and user personalization are separate capabilities, but they do not need to be separate autonomous agents.

Multi-agent behavior becomes justified only when the work naturally decomposes.

A future example:
- identify three plausible trade counterparties,
- launch separate trade-search workers for each manager,
- have a lead agent compare and synthesize their proposals.

### What should be deterministic software

Do not use agents for:
- league synchronization,
- game-status checks,
- scoring calculations,
- roster legality,
- persistence,
- scheduling,
- transaction ingestion,
- projection retrieval,
- notification delivery,
- manager-statistic calculation,
- obvious ranking/filtering.

### What should be agentic

Use the reasoning agent when the next useful step depends on what it discovers.

Example waiver investigation:

1. detect a backup RB usage spike,
2. inspect whether the starter was injured,
3. check injury reporting,
4. inspect snap/carry/target changes,
5. determine whether the backup is available,
6. inspect the user's roster,
7. inspect competing managers' RB needs and bidding tendencies,
8. determine whether a waiver recommendation is justified,
9. recommend or stay silent.

That is meaningfully agentic because the control flow is not fully known in advance.

---

## 11. Data feasibility: conclusions already established

Data feasibility should be treated as an understood project constraint rather than a future product-discovery task.

### ESPN access

There is no clean supported public ESPN Fantasy developer API for this use case.

The practical ecosystem uses ESPN's internal fantasy endpoints through community wrappers such as `espn-api`.

Private leagues typically require authenticated ESPN session values such as:
- `SWID`
- `espn_s2`

These credentials should be treated like secrets and kept server-side.

### Data that community tooling can expose

The current ecosystem can expose or help reconstruct:
- league settings,
- scoring settings,
- teams and owners,
- current rosters,
- matchups,
- standings,
- draft results,
- free agents,
- transactions,
- adds/drops,
- trades,
- recent league activity,
- waiver/free-agent auction information,
- FAAB-related data in supported contexts.

This is enough to make the core concept technically plausible.

### Historical limitations

Historical ESPN data is not uniformly reliable.

Known constraints include:
- recent-activity tooling is limited for older seasons and is notably weaker before 2019,
- pre-2018 seasons can use different historical endpoints,
- some historical box-score or transaction detail may be missing,
- historical trade records can have incomplete player legs,
- some older league seasons may fail to load,
- private historical access can depend on authenticated account history,
- undocumented ESPN endpoints can change.

Therefore the product should **not assume that every event since 2012/2013 can be reconstructed perfectly**.

The working assumption should be:

> recent seasons are much more useful for transaction-level leaguemate modeling, while older seasons may contribute broader league context such as standings, drafts, teams, and matchups where available.

The system should begin recording a clean event history prospectively so that future analysis does not depend on reconstructing ESPN history later.

### ESPN as an adapter

Architecturally, ESPN should be treated as a replaceable league provider:

```text
EspnLeagueProvider
    ↓
normalized internal league model
```

The rest of the application should reason over the internal representation rather than ESPN-specific response objects.

### Public-product caveat

A personal prototype and a public multi-user product are different problems.

A personal version can tolerate more brittle integration.

A public product would need a more deliberate authentication and platform-access strategy. The current community approach relies on undocumented endpoints and session credentials rather than a clean fantasy OAuth flow.

That should not block the personal MVP.

---

## 12. External football intelligence

Do not spend the initial project rebuilding projections.

The agent needs good inputs from existing football-data and expert systems.

Useful categories:

### League truth
- ESPN league state
- rosters
- settings
- transactions
- ownership
- waivers
- trades

### Football performance
- player stats
- play-by-play
- usage
- targets
- carries
- injuries
- depth-chart changes

### Forecasts and expert signals
- projections
- consensus rankings
- injury outlook
- analyst opinions
- schedule strength
- player news

### Candidate sources

For a personal prototype:
- ESPN adapter for league state,
- FantasyPros as a possible expert/projection baseline,
- nflverse/nflfastR-style data for historical football analytics,
- selective fresh news research only when a shortlisted situation needs it.

FantasyPros is especially useful as a **baseline** because the project is not trying to prove it can generate generic projections better than established fantasy products.

The interesting question is:

> **Does adding user-specific and league-specific intelligence improve the decisions beyond a strong generic baseline?**

---

## 13. Persistent memory and data model

There should be three conceptually separate memory systems.

### A. Football state

Timestamped facts about players and the NFL:
- current team,
- injury status,
- projections,
- recent usage,
- news,
- depth-chart position,
- recent performance.

### B. League memory

Facts and derived statistics about the user's league:
- manager history,
- roster history,
- transaction history,
- trade history,
- FAAB behavior,
- waiver behavior,
- player ownership intervals,
- draft history,
- roster churn,
- positional tendencies.

### C. User strategy memory

Preferences and inferred strategy:
- risk tolerance,
- FAAB tendencies,
- trade preferences,
- stash preferences,
- notification preferences,
- roster-construction preferences.

Every inferred preference should ideally have provenance and confidence.

### Recommendation history

Every recommendation should be persisted.

Conceptually:

```text
Recommendation
- league
- timestamp
- type
- triggering event
- recommendation
- alternatives
- evidence
- confidence / uncertainty
- whether user saw it
- whether user accepted it
- rejection reason
- actual action taken
- downstream outcome
```

Over time, this becomes one of the product's most valuable datasets.

---

## 14. Suggested internal schema

```text
users
leagues
managers
teams
players

league_settings
roster_snapshots
matchups
standings
draft_picks
league_transactions
waiver_events
trades
player_ownership_intervals

player_week_snapshots
projections
injury_snapshots
news_items

events
diffs
opportunity_candidates

user_preferences
manager_features

recommendations
recommendation_evidence
recommendation_feedback
observed_user_actions
agent_runs
```

PostgreSQL is sufficient for the core system. A vector database is not necessary for most of this because the core state is highly structured.

---

## 15. Product examples

### Waiver opportunity

```text
Waiver opportunity

Add Player X
Drop Player Y

Why:
X's route participation and targets jumped substantially this week,
and the starter ahead of him left injured. He is still available in
your league and fits your bench as an upside stash.

Suggested FAAB: $11
Confidence: Medium

Reply:
1. Too much FAAB
2. Don't believe the player
3. Prefer my current player
4. Other
```

### Trade opportunity

```text
Trade window

Consider targeting Player X from Matt.

Why now:
- Matt just lost his RB2.
- He has excess WR depth.
- You have excess RB depth.
- His previous trades suggest he is willing to exchange depth
  for an immediate positional starter.

Potential opening offer:
Player A + Player B for Player X

Trade value: favorable
Estimated acceptability: plausible
Confidence: medium
```

The two judgments should remain separate:

```text
Is this trade good for the user?
Is this trade plausible for this manager?
```

---

## 16. V1 scope

### Build first
- League A only
- ESPN only
- recommendation-only behavior
- current league state ingestion
- persistent snapshots/events
- user profile
- basic manager behavior features
- waiver recommendations
- basic trade discovery
- start/sit recommendations
- event/diff pipeline
- one reasoning agent with tools
- high-precision notification gate
- SMS
- recommendation feedback/history

### Add second
- League B
- cross-league manager modeling
- richer trade-counterparty analysis
- better preference learning
- retrospective evaluation
- more sophisticated news/player research

### Do not prioritize yet
- automatic roster actions
- polished web dashboard
- arbitrary fantasy platforms
- proprietary projection model
- large multi-agent framework
- complex autonomous memory system
- constant LLM monitoring

---

## 17. How this differs from FantasyPros

FantasyPros already handles much of generic fantasy intelligence:
- league syncing,
- rankings,
- projections,
- start/sit,
- waivers,
- trades,
- news,
- AI-assisted advice.

Therefore:

> **"AI fantasy assistant" is not enough of a project.**

The intended differentiation is the combination of:

1. **Longitudinal user personalization**  
   The system learns which kinds of rational fantasy moves the user actually prefers and why.

2. **Leaguemate modeling**  
   The system reasons about the behavior and current incentives of specific people in the user's league.

3. **Event-driven opportunity detection**  
   The system does not wait for the user to ask every question.

4. **High-precision abstention**  
   The product is allowed to determine that nothing is worth the user's attention.

5. **Persistent league context**  
   Recommendations incorporate what has happened previously in this exact league.

The concise positioning is:

> **FantasyPros understands fantasy football. This product should understand my league.**

---

## 18. Evaluation

The project should eventually compare itself against a strong generic baseline rather than judging recommendations by vibes.

### Recommendation quality
- Did the move improve expected roster value?
- Did a waiver pickup outperform the best realistic alternative?
- Was the suggested drop defensible?
- Did start/sit recommendations beat plausible alternatives?

### Personalization
- Does the user prefer personalized recommendations to generic baseline advice?
- When the agent intentionally deviates from consensus, is there a clear league/user-specific justification?
- Does feedback reduce repeated unwanted recommendations?

### Trade quality
Two separate evaluation problems:
1. Was the proposed trade favorable to the user?
2. Was the proposed offer actually plausible for the counterparty?

Trade acceptance is particularly valuable evidence for the leaguemate model.

### Notification precision
- How often was a notification genuinely actionable?
- How often was it ignored?
- How often did the user wish the agent had stayed silent?
- How often did the system miss an opportunity the user later cared about?

The product should optimize for **precision, not notification count**.

---

## 19. Current architectural decisions

Treat these as settled unless implementation teaches us otherwise.

1. Build for one user first.
2. Support ESPN only initially.
3. Start with League A.
4. Recommendations only, no automatic actions.
5. SMS-first UX.
6. Optimize for winning with soft personalization.
7. High precision, low notification volume.
8. Batch Sunday game data rather than reason on every event.
9. Most events update state cheaply and do not invoke an LLM.
10. Use one main reasoning agent initially.
11. Do not force multi-agent architecture.
12. Use deterministic code for obvious computation and filtering.
13. Focus differentiation on user + leaguemate modeling rather than rebuilding projections.
14. Persist recommendation history and feedback.
15. Treat ESPN as an adapter because its integration is unofficial/brittle.
16. Design internal models with `user_id` and `league_id` so multi-user support remains possible later.

---

## 20. Immediate build direction

The project is now past broad idea exploration.

The next work should focus on turning the above into a thin end-to-end vertical slice:

```text
ESPN league sync
→ normalized state
→ snapshot + diff
→ one concrete opportunity detector
→ reasoning agent
→ recommendation / NO_ACTION
→ text message
→ feedback persisted
```

A good first workflow is probably **waivers**, because it exercises:
- NFL/player data,
- league availability,
- roster fit,
- FAAB,
- user preferences,
- leaguemate competition,
- event-driven analysis,
- and feedback.

The first version does not need to be brilliant.

It needs to prove that the system can:

1. understand the real league,
2. detect a meaningful opportunity,
3. investigate it,
4. make a league-specific recommendation,
5. choose whether it is worth interrupting the user,
6. and learn from the response.

That is the core product loop.

---

## 21. Feasibility findings from the 2026 scan

The read-only feasibility experiment scanned seasons 2013–2026 and saved raw
responses in an ignored local run archive. The resulting coverage report and
transaction analysis are reproducible from the run ID.

### Confirmed capabilities

- Authenticated access to the private league works with ESPN session cookies.
- Settings, teams, owners, drafts, schedules, matchups, and weekly roster
  payloads were returned for every scanned season.
- Historical team counts vary even though the current league has 12 teams;
  team IDs must not be treated as permanent manager identities.
- Structured `mTransactions2` records are present from 2018 onward.
- The normalized 2018–2026 archive contains 5,772 unique transactions:
  - 1,697 waiver claims
  - 1,201 free-agent moves
  - 30 completed-trade events
  - 268 trade proposals
  - 56 trade declines/vetoes
  - 808 lineup/roster changes
  - 1,710 draft events
- FAAB amounts and failed waiver statuses are available from the 2018–2026
  transaction records where applicable.

### Historical limitations

- The 2013–2017 responses returned no `transactions` dataset key. They remain
  useful for roster, matchup, draft, settings, and standings context but are not
  transaction-complete.
- Cross-season manager joins require stable member IDs or manual confirmation and
  must carry uncertainty when ambiguous.
- The activity/communication feed returned current-season 2026 topics but returned
  404 for 2019–2025. Activity is optional current-season enrichment, not a
  historical source of truth.
- Trade and roster reconstruction still require representative manual checks
  against the ESPN UI before being treated as authoritative.
- ESPN access uses unofficial endpoints and private session credentials, so the
  league provider must remain replaceable and credentials must stay server-side.

### Updated modeling boundary

Use 2018 onward as the primary transaction and manager-behavior window. Treat
2013–2017 as partial historical context. Record clean prospective events so the
system does not depend on reconstructing every older ESPN action later.

### Current implementation status

The repository contains a credential-safe ESPN client, immutable raw archive,
resumable scanner, coverage analyzer, transaction normalizer, and opt-in live
integration tests. The next product work is managed persistence and normalized
League A state, not SMS or autonomous roster actions.

## 22. Phased implementation roadmap

### Phase 0 — Close the experiment

Keep raw runs private and reproducible, complete representative UI verification,
and document the 2018 transaction boundary. Activity remains optional.

### Phase 1 — Persistence foundation

Add managed PostgreSQL migrations for users, leagues, seasons, sync runs, raw
payload references, managers, identities, teams, settings, and scoring periods.
Add idempotent repository upserts and preserve source provenance.

### Phase 2 — Normalize League A

Load 2026 first, then 2018–2025, including rosters, matchups, drafts,
transactions, transaction items, and ownership intervals. Load older seasons only
as explicitly partial context.

### Phase 3 — Manager analytics

Compute measured waiver, FAAB, trade, lineup, roster-churn, draft, and player
holding features with sample sizes, observation windows, and confidence.

### Phase 4 — Opportunity and recommendation loop

Add football-data adapters, deterministic opportunity filtering, a single reasoning
orchestrator, and a CLI-first waiver workflow. Persist recommendations, evidence,
feedback, and outcomes. Add trades and start/sit after the waiver loop is measured.

### Phase 5 — Runtime and expansion

Add scheduled event windows, high-precision notification gates, an SMS adapter,
and finally League B using the same provider and schema.

---

## 23. Supabase load status and handoff (September 26, 2026)

The first completed ESPN scan (`20260923T011957Z`) has now been loaded into the
confirmed Supabase project through the Supabase MCP. The load is complete and
the importer is safe to rerun using natural-key upserts.

### Loaded row counts

| Entity | Rows |
| --- | ---: |
| users | 1 |
| leagues | 1 |
| league_seasons | 14 |
| managers | 32 |
| manager_season_identities | 164 |
| teams | 164 |
| league_settings | 14 |
| scoring_periods | 231 |
| sync_runs | 1 |
| raw_payloads | 736 |
| roster_snapshots | 2,536 |
| roster_entries | 38,346 |
| draft_picks | 2,454 |
| transactions | 5,772 |
| transaction_items | 9,914 |

### Important coverage note

`matchups` is currently empty. The archived `mSchedule` responses contained
status metadata but did not expose a usable schedule array in this scan, so the
loader correctly did not invent matchup rows. Matchup ingestion needs a separate
endpoint/response-shape investigation before manager performance features depend
on it.

### Persistence implementation

`scripts/load_run.py` loads the raw run, manifest metadata, league dimensions,
rosters, drafts, transactions, and transaction items. It preserves raw payload
paths and hashes in `raw_payloads`, stores provider IDs alongside internal UUIDs,
and converts ESPN epoch-millisecond dates to UTC timestamps. The database remains
backend-owned: public tables have RLS enabled and Data API access is revoked until
explicit application policies are designed.

The local database helper supports `DATABASE_HOSTADDR` for environments where the
Supabase database hostname publishes only an IPv6 address and local DNS cannot
resolve it. This is a connectivity workaround, not a replacement for the
hostname; keep the hostname in `DATABASE_URL` for TLS/SNI.

### Immediate next steps

1. Add a repository/query layer so application code does not issue ad hoc SQL.
2. Validate row-level invariants: one season per year, roster uniqueness, one
   transaction per provider ID, and transaction-item referential integrity.
3. Investigate and load matchups from the correct ESPN endpoint shape.
4. Add point-in-time manager feature tables for waivers, FAAB, trades, lineup
   changes, draft behavior, and holding periods.
5. Add migrations for opportunities, recommendations, evidence, feedback, and
   outcomes only after the normalized state queries are stable.

Do not treat the presence of rows in Supabase as proof that historical coverage
is complete. The 2018 transaction boundary, uncertain cross-season identity joins,
and missing matchup payloads remain explicit modeling constraints.
