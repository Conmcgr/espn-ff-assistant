CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS leagues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    provider_league_id BIGINT NOT NULL,
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider, provider_league_id)
);

CREATE TABLE IF NOT EXISTS league_seasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_id UUID NOT NULL REFERENCES leagues(id),
    season INTEGER NOT NULL,
    availability_state TEXT NOT NULL DEFAULT 'unknown',
    first_scoring_period INTEGER,
    final_scoring_period INTEGER,
    source_payload_id UUID,
    UNIQUE (league_id, season)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_id UUID NOT NULL REFERENCES leagues(id),
    run_key TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error TEXT,
    UNIQUE (league_id, run_key)
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    season INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    scoring_period INTEGER,
    payload_uri TEXT,
    payload_sha256 TEXT,
    transport_state TEXT NOT NULL,
    retrieved_at TIMESTAMPTZ,
    UNIQUE (sync_run_id, season, view_name, scoring_period, payload_sha256)
);

CREATE TABLE IF NOT EXISTS managers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    provider_member_id TEXT NOT NULL,
    display_name TEXT,
    UNIQUE (user_id, provider, provider_member_id)
);

CREATE TABLE IF NOT EXISTS manager_season_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    manager_id UUID REFERENCES managers(id),
    provider_team_id BIGINT,
    identity_confidence TEXT NOT NULL DEFAULT 'unknown',
    source_payload_id UUID,
    UNIQUE (league_season_id, provider_team_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_team_id BIGINT NOT NULL,
    name TEXT,
    UNIQUE (league_season_id, provider_team_id)
);

CREATE TABLE IF NOT EXISTS league_settings (
    league_season_id UUID PRIMARY KEY REFERENCES league_seasons(id),
    settings JSONB NOT NULL,
    source_payload_id UUID
);

CREATE TABLE IF NOT EXISTS scoring_periods (
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    scoring_period INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (league_season_id, scoring_period)
);

-- These tables are backend-owned. Keep them inaccessible through the Data API
-- until explicit authenticated-user policies are designed.
REVOKE ALL ON TABLE
    users,
    leagues,
    league_seasons,
    sync_runs,
    raw_payloads,
    managers,
    manager_season_identities,
    teams,
    league_settings,
    scoring_periods
FROM anon, authenticated;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE leagues ENABLE ROW LEVEL SECURITY;
ALTER TABLE league_seasons ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_payloads ENABLE ROW LEVEL SECURITY;
ALTER TABLE managers ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_season_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE league_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE scoring_periods ENABLE ROW LEVEL SECURITY;
