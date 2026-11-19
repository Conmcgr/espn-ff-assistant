# ESPN Fantasy Football Assistant

## Notification policy (outbox only)

`uv run python scripts/worker.py --league ID --user UUID --team ID --season YEAR --week N --outbox`
opts into atomic recommendation/evidence + delivery-intent persistence. Without
`--outbox`, the worker retains its shadow-only behavior. Nothing sends SMS.
JSON includes each record's `send`, `batch`, or `suppress` disposition and reason.
`--now` accepts an aware ISO timestamp for deterministic fixture replays.

Defaults are quiet hours **22:00–09:00 America/Chicago** and **2** immediate
non-urgent notifications per local day. Set `quiet_hours` and
`max_daily_notifications` in user preferences to override; invalid values fall
back to defaults. Urgent actions bypass quiet hours and caps; a notify action may
escalate once to urgent. Duplicate actions do not requeue. Non-actionable records
are saved as recommendations but never create delivery intents.

See [the outbox contract](docs/notification-outbox.md) for TTL, dedupe and the
future sender/reply integration boundaries.

Persistence and manager-analytics foundation for the proactive fantasy-football
agent described in [the project handoff](fantasy_football_agent_handoff_detailed.md).

The data-feasibility spike is complete. The current phase normalizes League A
historical state into managed PostgreSQL (Supabase), computes manager behavior
features, and prepares the query layer needed by the recommendation loop.

## Setup

Requirements:

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- Access to the private ESPN league (for re-scanning only)
- A PostgreSQL database (`DATABASE_URL` in `.env`)

```bash
cp .env.example .env
# fill in ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID, DATABASE_URL
uv sync --extra dev
uv run python scripts/check_setup.py
uv run pytest
```

## Repository layout

```text
src/espn_ff_assistant/          reusable extraction, normalization, repository
src/espn_ff_assistant/normalize/  pure payload-to-rows parsers (no I/O)
scripts/                        thin command-line entry points
tests/                          unit tests (synthetic fixtures) + DB integration
migrations/                     numbered SQL migrations, applied once
data/raw/                       Git-ignored, immutable ESPN responses
data/derived/                   Git-ignored, generated/private datasets
reports/                        coverage and verification outputs
docs/                           architecture and roadmap
```

## Database setup

Apply all migrations (idempotent on re-run):

```bash
uv run python scripts/migrate.py
```

If your Supabase host resolves only to an IPv6 address that your local DNS
cannot reach, set `DATABASE_HOSTADDR` to the raw IP. Keep `DATABASE_URL`
pointing at the hostname so TLS SNI still works.

## Loading data

Load a completed ESPN scan into the database:

```bash
uv run python scripts/load_run.py data/raw/20260923T011957Z
```

The loader is idempotent — safe to rerun after a migration or code change.
Use `--only core` or `--only player_state` to load just those sections.

## Recommendations

```bash
uv run python scripts/recommend.py sync                                  # read-only current-week fetch + load
uv run python scripts/recommend.py whoami --season 2026 --team <id>      # one-time: which team is yours
uv run python scripts/recommend.py prefs --set streams_dst=true --source onboarding
uv run python scripts/recommend.py scan --season 2026 --week 4 --save    # lineup + waivers, persisted
uv run python scripts/recommend.py explain <recommendation_id>
uv run python scripts/recommend.py feedback <recommendation_id> --action rejected --reason too_much_faab
uv run python scripts/score_outcomes.py --season 2026 --week 4           # after the week completes
uv run python scripts/backtest.py                                        # private report in data/derived/
```

See `docs/recommendation-engine-handoff.md` for design, calibration, and
known gaps.

### One-shot shadow worker

```bash
uv run python scripts/worker.py --league <espn_id> --user <users.id_uuid> --team <espn_team_id> --season 2026 --week 4
```

Use the configured `ESPN_LEAGUE_ID`, the existing primary user's UUID, and
their saved team mapping for the selected season. The week must match ESPN's
current scoring period. The worker archives the run privately and loads it
only if all required views are usable and were retrieved within 15 minutes.
An explicit empty transaction list or free-agent pool is valid; an absent
list, failed request, or partial roster is not.

The command prints one JSON result. `ACTIONABLE` and `NO_ACTION` both exit 0;
sync/evaluation failures exit 1 and invalid configuration or scope exits 2.
`alert_eligible` means the engines produced an `urgent` or `notify` decision
from validated inputs. This command does not send alerts or save recommendations;
the manual recommendation commands are unchanged.

## Validation

Check row-level invariants after any load:

```bash
uv run python scripts/validate_db.py
```

Exits non-zero on hard failures. Review soft warnings to catch data gaps.

## Rebuilding coverage and transaction reports

```bash
uv run python scripts/rebuild_reports.py YOUR_RUN_ID
```

## Development checks

```bash
uv run ruff check .
uv run pytest
```

Integration tests against real ESPN (read-only, requires `.env`):

```bash
RUN_ESPN_INTEGRATION=1 uv run pytest tests/integration/test_espn_access.py -v
```

Database integration tests (requires `TEST_DATABASE_URL`):

```bash
TEST_DATABASE_URL=postgresql://... uv run pytest tests/integration/ -v
```

## Re-scanning ESPN

```bash
RUN_ESPN_INTEGRATION=1 uv run python scripts/scan_seasons.py
# or for a single season smoke test:
RUN_ESPN_INTEGRATION=1 uv run python scripts/scan_seasons.py --season 2026
# resume a partial run:
RUN_ESPN_INTEGRATION=1 uv run python scripts/scan_seasons.py --resume RUN_ID
```

Raw responses are written under `data/raw/`; coverage matrix lands in
`reports/coverage.csv`.
