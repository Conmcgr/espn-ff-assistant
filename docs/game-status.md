# F3: NFL game-status adapter

`espn_ff_assistant.game_status.normalize_game_status` consumes the existing
`proTeamSchedules_wl` fantasy season response. `sync_current` already fetches it
with `game_level=True`; `RawArchive` saves its payload and manifest retrieval
timestamp. No second feed or network call is introduced.

## Source contract and limits

The outer shape is established by `normalize/players.py:pro_schedule` and the
resolved schedule finding in `docs/recommendation-engine-handoff.md` §649:
`settings.proTeams[].proGamesByScoringPeriod[period][]`, game `id`, and `date`
(epoch milliseconds). The same game appears under both participating teams.
Team ID 0 is ignored. Byes create no game-status records.

**Unverified assumption:** each game exposes a scalar `status` containing ESPN
`STATUS_SCHEDULED`, `STATUS_IN_PROGRESS`, `STATUS_DELAYED`, `STATUS_POSTPONED`,
`STATUS_SUSPENDED`, or `STATUS_FINAL` names. The existing checked-in code proves
the schedule structure, **not this status field or its vocabulary**. All test
data is synthetic. No credentialed/live response was available for validation.
Numeric, object-shaped, absent, and unrecognized statuses remain `unknown`;
in particular, scoreboard-shaped `status.type` objects are not silently assumed
to be the fantasy schema. Validate this field against a private archived/live
response before using finality in production. This PR does not establish that
the live fantasy shape exposes usable status, nor justify adding another feed.

## Usage and temporal semantics

Pass the retrieval timestamp, not kickoff or the current clock, as an aware
`datetime`. For archived data use manifest `retrieved_at`; for fresh fetches use
`datetime.fromisoformat(result.retrieved_at)` and pass `fetch_state=result.state`.
The batch distinguishes missing, empty, malformed, partial, present, and failed
input. Unknown status is a valid game observation, not a missing game. Invalid
entries are reported in `issues`; conflicting duplicate IDs are fail-closed to
unknown. Each returned dictionary is keyed by provider game ID (string).

`batch.games[id].final_observed_at` is the retrieval timestamp only when that
observation explicitly says final; otherwise it is `None`. It is not an actual
game-end timestamp. Kickoff is never completion evidence. Preserve individual
batches to retain history; the adapter does not merge observations or claim an
earliest final time. R1 must select observations as of its review time and track
the first final observation/settling delay, including subsequent corrections.

No schema change: `pro_team_games` currently stores schedule/bye information,
not observation history. The pure boundary and existing raw archives suffice
for F3; durable completion-review state belongs with R1 rather than overwriting
schedule rows or inventing finality history here.

## Offline demo

From the repository root:

```sh
uv run --offline python -c 'import json; from datetime import datetime; from espn_ff_assistant.game_status import normalize_game_status; b = normalize_game_status(json.load(open("tests/fixtures/game_status.json")), observed_at=datetime.fromisoformat("2026-09-28T02:00:00+00:00")); print(b.state); [print(g.provider_game_id, g.state.value, g.final_observed_at) for g in b.games.values()]'
```
