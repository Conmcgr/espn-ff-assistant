-- 004: players table, co-owner support, source provenance on all rows

-- Player identity table populated from roster payloads.
CREATE TABLE IF NOT EXISTS players (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_player_id BIGINT NOT NULL UNIQUE,
    full_name TEXT,
    first_name TEXT,
    last_name TEXT,
    default_position_id INTEGER,
    pro_team_id INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Co-owner support: multiple owners per team in a season.
-- Replaces the single-manager assumption in manager_season_identities.
CREATE TABLE IF NOT EXISTS team_owners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_team_id BIGINT NOT NULL,
    manager_id UUID NOT NULL REFERENCES managers(id),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (league_season_id, provider_team_id, manager_id)
);

CREATE INDEX IF NOT EXISTS team_owners_season_team_idx
    ON team_owners (league_season_id, provider_team_id);
CREATE INDEX IF NOT EXISTS team_owners_manager_idx
    ON team_owners (manager_id);

-- Add provider_matchup_id to matchups so the unique key is stable.
-- Drop the old composite unique constraint and replace it.
ALTER TABLE matchups ADD COLUMN IF NOT EXISTS provider_matchup_id BIGINT;
ALTER TABLE matchups ADD COLUMN IF NOT EXISTS period_type TEXT NOT NULL DEFAULT 'regular';
ALTER TABLE matchups ADD COLUMN IF NOT EXISTS winner TEXT;
ALTER TABLE matchups ADD COLUMN IF NOT EXISTS is_bye BOOLEAN NOT NULL DEFAULT FALSE;

-- Source provenance: raw payload URI on every fact table.
-- Using TEXT so no FK constraint is required (payloads are file-backed).
ALTER TABLE roster_snapshots ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT;
ALTER TABLE draft_picks ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT;
ALTER TABLE matchups ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT;
ALTER TABLE players ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT;

-- Unique index on matchups.provider_matchup_id (sparse; old rows have NULL).
CREATE UNIQUE INDEX IF NOT EXISTS matchups_provider_matchup_id_idx
    ON matchups (provider_matchup_id)
    WHERE provider_matchup_id IS NOT NULL;

REVOKE ALL ON TABLE players, team_owners FROM anon, authenticated;
ALTER TABLE players ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_owners ENABLE ROW LEVEL SECURITY;
