# Deploying the shadow worker (R2)

## GitHub Actions (current host)

`.github/workflows/shadow-worker.yml` runs one tick, then the health check,
every 15 minutes through the game windows (Thursday through Monday night,
UTC) and hourly otherwise. Public-repo Actions minutes are free. Every run
fails if the health check finds missed runs (no tick for 120 min), a stale
scan (8 h) or 3 failed ticks in a row. A failed tick on its own does not fail
the run. GitHub emails the failure to whoever last changed the schedule.

The repository is public, so its run logs are public too. The tick only
prints status lines, and GitHub masks secret values. Raw ESPN payloads go
gzipped to the private `raw_objects` table (`RAW_ARCHIVE_URL=db:///raw`,
RLS on, no Data API access), never to Actions artifacts. A daily run prunes
raw runs older than 7 days, keeping the latest 10: a scan archives about 2 MB gzipped, so a week is roughly 100 MB of the 500 MB free database.

Repository secrets ([Settings → Secrets and variables → Actions](https://github.com/Conmcgr/espn-ff-assistant/settings/secrets/actions)):
`ESPN_LEAGUE_ID`, `ESPN_S2`, `ESPN_SWID`, `DATABASE_URL_RUNTIME` (the
`ff_runtime` session-pooler URL), `WORKER_USER_ID`, `WORKER_TEAM_ID`.

To run it by hand, use **Actions → Shadow worker → Run workflow** (tick
`dry_run` to plan only), or `gh workflow run shadow-worker.yml -f dry_run=true`.
To pause it, run `gh workflow disable shadow-worker.yml`. GitHub also
disables schedules after 60 days without repository activity.

GitHub can delay scheduled runs at busy times or skip them. That is fine
while nothing is texted, but revisit the host before urgent pre-lock texts
go live.

## Cloud Run Jobs (alternative)

The worker can also run as two Cloud Run Jobs, started by Cloud Scheduler in
`America/New_York`:

| Job | Schedule | Does |
| --- | --- | --- |
| `ff-tick` | every 10 min | `scripts/scheduled_tick.py`: reads league status and the public scoreboard, asks the trigger policy what is due, runs one worker scan when a trigger fires or the last scan is older than `WORKER_REFRESH_HOURS` (6), records fired triggers and a `worker_ticks` heartbeat |
| `ff-health` | :05 and :35 | `scripts/health_check.py`: logs `ERROR` and exits 1 on missed runs (no tick for 45 min), a stale sync (no scan for 8 h) or 3 failed ticks in a row |

It is shadow mode only: nothing is sent and ESPN is only read. The tick
refuses to start if `DELIVERY_ENABLED`, `SMS_ENABLED` or
`ESPN_ACTIONS_ENABLED` is set to anything but false, and the deploy script
pins all three to false. A database advisory lock makes overlapping ticks
skip (`SKIPPED_OVERLAP`) instead of scanning twice.

## Roles

Migrations and the runtime use different database roles:

- **Release (owner) role** — `DATABASE_URL` on an operator machine. Runs
  `uv run python scripts/migrate.py` once per release, before deploying.
  Never given to Cloud Run.
- **Runtime role `ff_runtime`** — created `NOLOGIN` by migration 016 with
  DML on the application tables and no DDL or `schema_migrations` writes.
  Cloud Run receives only `DATABASE_URL_RUNTIME`, which connects as it.

Enable it once, with a password generated on your machine (never committed):

```sql
ALTER ROLE ff_runtime LOGIN PASSWORD '<generated>';
```

On Supabase, connect through the session pooler as
`ff_runtime.<project-ref>` (Cloud Run has no IPv6 route to the direct host).

## One-time setup

1. Create a GCP project with billing, and a private bucket for raw payloads
   (uniform access, public access prevention on).
2. Create the secrets (values come from stdin, never the shell history):

   ```bash
   for s in ESPN_LEAGUE_ID ESPN_S2 ESPN_SWID DATABASE_URL_RUNTIME; do
     gcloud secrets create "$s" --replication-policy automatic
     read -rsp "$s: " v && printf %s "$v" | gcloud secrets versions add "$s" --data-file=- ; echo
   done
   ```

3. Apply migrations with the owner role, then deploy:

   ```bash
   uv run python scripts/migrate.py
   PROJECT=<project> WORKER_USER_ID=<users.id> WORKER_TEAM_ID=<team> \
     RAW_ARCHIVE_URL=gs://<bucket>/raw ALERT_EMAIL=<you> ./deploy/gcp/deploy.sh
   ```

   `DRY_RUN=1` prints every command without running it.

   With `ALERT_EMAIL` set, the script also creates an email channel and the
   alert policy in `deploy/gcp/alert-policy.json`. It fires on any `ERROR`
   log line from either job (failed tick, unhealthy check) and when the
   health job itself stops completing.

## Verify before trusting it

```bash
# Plan only: reads ESPN and the DB, writes nothing, runs no scan.
gcloud run jobs execute ff-tick --region us-central1 --wait \
  --args scripts/scheduled_tick.py,--dry-run
gcloud run jobs execute ff-tick --region us-central1 --wait
gcloud run jobs execute ff-health --region us-central1 --wait
```

Then compare one recent scan against ESPN and watch one full
game-to-waiver cycle in the job logs before any delivery work starts.

## Rollback

- **Stop everything:** `gcloud scheduler jobs pause ff-tick ff-health --location us-central1`.
  Nothing else depends on the worker, so pausing is always safe.
- **Bad release:** redeploy the previous image tag:
  `gcloud run jobs update ff-tick --image <region>-docker.pkg.dev/<project>/ff-agent/worker:<previous-sha>`
  (same for `ff-health`), or rerun `deploy.sh` with `TAG=<previous-sha>`.
- **Schema:** migration 016 only adds `worker_ticks` and the `ff_runtime`
  role, so older images keep working. To revoke runtime access:
  `ALTER ROLE ff_runtime NOLOGIN;`.
- **Off-season:** pause both scheduler jobs.
