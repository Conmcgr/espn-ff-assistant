-- 010: player projections/actuals, player status and availability, pro schedule.
--
-- Each ESPN payload is a snapshot taken during `snapshot_period`; it carries
-- stats for several `scoring_period`s (0 = season level). Projections move
-- during a week, so rows are kept per sync run and read point-in-time.

CREATE TABLE IF NOT EXISTS player_week_stats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_player_id BIGINT NOT NULL,
    snapshot_period INTEGER NOT NULL,
    scoring_period INTEGER NOT NULL,
    stat_source TEXT NOT NULL,          -- 'actual' | 'projected'
    stat_split TEXT NOT NULL,           -- 'week' | 'season' | 'season_current'
    applied_total NUMERIC,
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    retrieved_at TIMESTAMPTZ,
    raw_payload_uri TEXT,
    UNIQUE (league_season_id, provider_player_id, snapshot_period, scoring_period,
            stat_source, stat_split, sync_run_id)
);

CREATE INDEX IF NOT EXISTS player_week_stats_lookup_idx
    ON player_week_stats (league_season_id, scoring_period, stat_source, stat_split);

CREATE TABLE IF NOT EXISTS player_status_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_player_id BIGINT NOT NULL,
    snapshot_period INTEGER NOT NULL,
    injury_status TEXT,
    eligible_slots INTEGER[],
    default_position_id INTEGER,
    pro_team_id INTEGER,
    percent_owned NUMERIC,
    percent_started NUMERIC,
    percent_change NUMERIC,
    availability TEXT NOT NULL,         -- 'rostered' | 'free_agent' | 'waivers' | 'unknown'
    on_provider_team_id BIGINT,
    waiver_clear_at TIMESTAMPTZ,
    lineup_locked BOOLEAN,
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    retrieved_at TIMESTAMPTZ,
    raw_payload_uri TEXT,
    UNIQUE (league_season_id, provider_player_id, snapshot_period, sync_run_id)
);

CREATE INDEX IF NOT EXISTS player_status_snapshots_lookup_idx
    ON player_status_snapshots (league_season_id, snapshot_period, availability);

CREATE TABLE IF NOT EXISTS pro_team_games (
    season INTEGER NOT NULL,
    scoring_period INTEGER NOT NULL,
    pro_team_id INTEGER NOT NULL,
    opponent_pro_team_id INTEGER,
    is_home BOOLEAN,
    kickoff_at TIMESTAMPTZ,
    is_bye BOOLEAN NOT NULL DEFAULT false,
    provider_game_id BIGINT,
    PRIMARY KEY (season, scoring_period, pro_team_id)
);

REVOKE ALL ON TABLE player_week_stats, player_status_snapshots, pro_team_games
    FROM anon, authenticated;
ALTER TABLE player_week_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_status_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE pro_team_games ENABLE ROW LEVEL SECURITY;
