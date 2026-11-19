# F3: NFL game-status observations

## Validated ESPN sources

Live public responses checked on 2026-11-19:

- Fantasy season endpoint, `?view=proTeamSchedules_wl`:
  `settings.proTeams[].proGamesByScoringPeriod[period][]` has `id`, millisecond
  `date`, `statsOfficial`, `validForLocking`, `startTimeTBD`, team IDs and
  scoring period, **no status field**. This confirms the schedule structure used
  by `normalize/players.py:pro_schedule`, but disproves the original status-token
  assumption. Only literal `statsOfficial=true` is final evidence in
  `normalize_fantasy_schedule`; otherwise state is unknown.
- Public `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`,
  parameters `dates=2026&seasontype=2&week=N`: `events[].id`, ISO `date`, and
  `status.type.{name,state,completed}`. Weeks 1, 8 and 10 returned respectively
  16, 14 and 14 `STATUS_FINAL/post/true` events. Current week 11 returned 13
  `STATUS_SCHEDULED/pre/false` events. IDs share the fantasy game-ID namespace.

The fantasy feed cannot express interruptions, so the plan's failed-source
validation exception now justifies the second ESPN endpoint. `fetch_scoreboard`
uses a separate session with environment authentication disabled; it never uses
fantasy settings, cookies, or authorization. It returns FetchResult metadata,
including season type (week is `scoring_period`). Existing fantasy sync remains
unchanged; scoreboard ingestion is opt-in via the script.

## Contract

`normalize_game_status` is the primary scoreboard adapter. Final requires an
explicit allowlisted final name **and** `state=post` **and** literal
`completed=true`. Scheduled requires pre/false; in-progress, halftime,
end-period and overtime require in/false. Delayed/rain-delay, postponed and
suspended require pre or in, with completed=false. Canceled, missing, unmapped
and inconsistent status is unknown. Interruption/OT names are covered by
synthetic tests, not observed in the live samples above.

Both pure adapters return batches distinguished as missing, empty, malformed,
partial, present or failed. Records are keyed by string provider game ID and
include source, raw status evidence, UTC observed_at and kickoff_at. Conflicting
duplicates fail closed to unknown. Fantasy team 0 and byes produce no game.

Pass the actual aware retrieval timestamp: FetchResult.retrieved_at or an archive
manifest timestamp, never now() for old data. `final_observed_at` is evidence
time for that observation, not actual end-of-play or earliest-ever finality.
Keep batches separately; R1 owns as-of selection, corrections, first-final
tracking and settling delay. Kickoff is never finality evidence.

No migration: schedule rows are not observation history. Existing raw archival
conventions plus the pure adapter suffice here; R1 can add durable review state.
No live delayed/postponed/suspended or final-OT response was verified.

## Demo

```sh
uv run --offline python scripts/game_status.py --season 2026 --week 10
uv run --offline python scripts/game_status.py --season 2026 --week 11
uv run --offline python scripts/game_status.py --file tests/fixtures/scoreboard.json --observed-at 2026-09-28T02:00:00+00:00
uv run --offline python scripts/game_status.py --fantasy --file tests/fixtures/fantasy_schedule.json --observed-at 2026-09-28T02:00:00+00:00 --summary
```

Saved files require an explicit retrieval timestamp to avoid inventing evidence.
Fixtures are synthetic and contain no private league data. The script does not
persist raw responses or write to ESPN.
