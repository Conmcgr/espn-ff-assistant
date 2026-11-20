-- The pre-migration rows have no reliable retrieval timestamp. Keep them
-- available for week-only reads, but never use them for cutoff-based reads.
ALTER TABLE roster_snapshots
    ADD COLUMN sync_run_id UUID REFERENCES sync_runs(id),
    ADD COLUMN retrieved_at TIMESTAMPTZ,
    DROP CONSTRAINT roster_snapshots_league_season_id_scoring_period_provider_t_key,
    ADD CONSTRAINT roster_snapshots_run_key
        UNIQUE (league_season_id, scoring_period, provider_team_id, sync_run_id);

CREATE INDEX roster_snapshots_cutoff_idx
    ON roster_snapshots (league_season_id, provider_team_id, scoring_period DESC,
                         retrieved_at DESC, sync_run_id DESC)
    WHERE retrieved_at IS NOT NULL;

CREATE VIEW latest_roster_snapshots AS
    SELECT DISTINCT ON (league_season_id, scoring_period, provider_team_id)
        id, league_season_id, scoring_period, provider_team_id
    FROM roster_snapshots
    ORDER BY league_season_id, scoring_period, provider_team_id,
             retrieved_at DESC NULLS LAST, sync_run_id DESC NULLS LAST;

REVOKE ALL ON TABLE latest_roster_snapshots FROM PUBLIC, anon, authenticated;
