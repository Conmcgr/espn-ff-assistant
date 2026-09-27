# Agent guidance

## Mission

This repository builds the persistence and manager-analytics foundation for the
proactive fantasy-football agent described in
`fantasy_football_agent_handoff_detailed.md`. Read that file before making
product or architecture decisions.

The current phase covers:

1. **Persistence foundation** — managed PostgreSQL migrations, idempotent loaders,
   and a repository query layer with no ad-hoc SQL outside that layer.
2. **Normalized League A state** — rosters, matchups, drafts, transactions,
   transaction items, player ownership intervals.
3. **Manager analytics** — measured waiver, FAAB, trade, roster-churn, lineup,
   draft, and holding-period features with sample sizes and confidence.
4. **Deterministic recommendations** — ingest provider projections (ESPN
   first), compute start/sit and waiver recommendations (or `NO_ACTION`), and
   persist every recommendation, its evidence, feedback, and outcomes. See
   `docs/recommendation-engine-handoff.md`.

The ESPN data-feasibility spike is complete. Do not re-litigate it.

## Priorities

1. Keep all existing data security and read-only rules (see below).
2. Numbered SQL migrations, applied once, never edited after merge.
3. All database reads and writes go through `src/espn_ff_assistant/repository.py`
   — no SQL in scripts or tests outside that module and the migrations.
4. Loaders must be safely rerunnable: upserts on natural keys, child rows
   cleared and reloaded, no silent data loss.
5. All time-sensitive reads (roster state, manager stats) require an `as_of`
   (season, week) argument and must not return data from after that point.
6. Manager features are numbers with sample sizes and observation windows; no
   inferred psychological labels.
7. Provider projections are consumed as inputs. Building a proprietary
   projection model, LLM workflows, SMS, dashboards, deployment, and ESPN
   write actions remain out of scope.

## Data and security rules

- Never commit `ESPN_S2`, `SWID`, cookies, authorization headers, or `.env`.
- Never log credential values. Error messages may name a missing variable but
  must not print its contents.
- Raw ESPN responses are private (they contain member identifiers). Store only
  under `data/raw/`, which is Git-ignored.
- Generated/private analyses go under `data/derived/` or files ending in
  `.local.md`.
- Checked-in fixtures must be synthetic or thoroughly anonymized.
- Do not make roster moves, submit claims, propose trades, or call write
  endpoints. This repository is read-only with respect to ESPN.
- Database credentials (`DATABASE_URL`, `DATABASE_HOSTADDR`) are secrets;
  treat them like ESPN cookies.

## Database rules

- Migrations live in `migrations/` with a three-digit prefix (`001_`, `002_`,
  …). Apply with `uv run python scripts/migrate.py`.
- Never edit a migration after it has been applied to any environment; add a
  new migration instead.
- Schema changes required by a feature must be in a migration committed in
  the same pull request as the code that uses them.
- The query layer (`src/espn_ff_assistant/repository.py`) is the only place
  allowed to issue SQL against the application tables.
- The Supabase project has RLS enabled and Data API access revoked on all
  application tables. Do not weaken these settings.
- Tests that touch a real database must use `TEST_DATABASE_URL` and skip when
  it is not set.

## Engineering conventions

- Support Python 3.11 or newer.
- Use `uv` for dependency management when available.
- Put reusable code in `src/espn_ff_assistant/`, thin entry points in
  `scripts/`, and tests in `tests/`.
- Preserve raw payloads plus request metadata (season, view, scoring period,
  retrieval time, HTTP status), but never persist request cookies.
- Model missing, empty, partial, and failed responses as distinct states.
- Prefer stable ESPN IDs. Do not assume a team ID identifies the same manager
  across seasons.
- Keep source changes small and test them with `uv run pytest` and
  `uv run ruff check .`.

## Scope boundaries

Do not add SMS, LLM workflows, a proprietary projection model, dashboards,
deployment, or automated ESPN actions unless the user changes the scope
explicitly. Ingesting provider projections and deterministic recommendation
logic are in scope.
