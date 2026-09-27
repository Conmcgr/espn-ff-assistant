-- 005: player ownership intervals and manager feature tables

-- Player ownership intervals derived from weekly roster snapshots.
-- Built by the stats computation script; do not write directly from the loader.
CREATE TABLE IF NOT EXISTS player_ownership_intervals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_team_id BIGINT NOT NULL,
    provider_player_id BIGINT NOT NULL,
    start_week INTEGER NOT NULL,
    end_week INTEGER,               -- NULL if player still on roster at season end
    acquisition_type TEXT,          -- DRAFT, WAIVER, FREE_AGENT, TRADE, etc.
    end_reason TEXT,                -- drop, trade, season_end, unknown
    -- Optional FK to the transaction that started/ended this interval (2018+).
    start_transaction_id UUID REFERENCES transactions(id),
    end_transaction_id UUID REFERENCES transactions(id),
    UNIQUE (league_season_id, provider_team_id, provider_player_id, start_week)
);

CREATE INDEX IF NOT EXISTS poi_season_team_idx
    ON player_ownership_intervals (league_season_id, provider_team_id);
CREATE INDEX IF NOT EXISTS poi_player_idx
    ON player_ownership_intervals (provider_player_id);

-- Manager feature store: one row per (manager, league, stat, as_of, version).
-- Computed at end-of-season and at current week. Never mutate; always upsert.
CREATE TABLE IF NOT EXISTS manager_features (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manager_id UUID NOT NULL REFERENCES managers(id),
    league_id UUID NOT NULL REFERENCES leagues(id),
    stat_name TEXT NOT NULL,
    value NUMERIC,
    sample_size INTEGER,
    season_from INTEGER,            -- earliest season included in the window
    season_to INTEGER,              -- latest season included in the window
    as_of_season INTEGER NOT NULL,  -- season as of which this was computed
    as_of_week INTEGER NOT NULL,    -- scoring period as of which this was computed
    confidence TEXT,                -- 'high'/'medium'/'low' based on sample_size
    version INTEGER NOT NULL DEFAULT 1,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manager_id, league_id, stat_name, as_of_season, as_of_week, version)
);

CREATE INDEX IF NOT EXISTS manager_features_manager_idx
    ON manager_features (manager_id, league_id);
CREATE INDEX IF NOT EXISTS manager_features_stat_idx
    ON manager_features (stat_name, as_of_season);

REVOKE ALL ON TABLE player_ownership_intervals, manager_features
    FROM anon, authenticated;
ALTER TABLE player_ownership_intervals ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_features ENABLE ROW LEVEL SECURITY;
