# ESPN Fantasy Football Assistant

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
