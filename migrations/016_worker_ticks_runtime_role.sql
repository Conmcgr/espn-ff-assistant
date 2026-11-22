-- 016: scheduled tick heartbeat and a least-privilege runtime role.
-- The deployed worker connects as ff_runtime; migrations keep using the
-- owner role. ff_runtime is created NOLOGIN: the operator enables login and
-- sets its password out of band, never in a migration or the repository.

CREATE TABLE worker_ticks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    ok BOOLEAN NOT NULL,
    scoring_period INTEGER,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    worker_status TEXT,
    worker_run_id TEXT,
    error TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX worker_ticks_recent ON worker_ticks (league_season_id, started_at DESC);

REVOKE ALL ON TABLE worker_ticks FROM anon, authenticated;
ALTER TABLE worker_ticks ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ff_runtime') THEN
        -- BYPASSRLS keeps RLS (which has no policies) closed to anon/authenticated.
        CREATE ROLE ff_runtime NOLOGIN BYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO ff_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ff_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ff_runtime;
REVOKE INSERT, UPDATE, DELETE ON TABLE schema_migrations FROM ff_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ff_runtime;
