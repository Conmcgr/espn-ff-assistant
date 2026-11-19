CREATE TABLE opportunity_scans (
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (league_season_id, sync_run_id)
);

CREATE TABLE opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    kind TEXT NOT NULL CHECK (kind IN ('player_dropped', 'starter_out')),
    change_key TEXT NOT NULL,
    provider_player_id BIGINT NOT NULL,
    provider_team_id BIGINT,
    source_view TEXT NOT NULL,
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    first_observed_at TIMESTAMPTZ NOT NULL,
    last_observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attrs JSONB NOT NULL,
    expired_at TIMESTAMPTZ,
    UNIQUE (league_season_id, change_key)
);

CREATE TABLE opportunity_events (
    opportunity_id UUID NOT NULL REFERENCES opportunities(id),
    sync_run_id UUID NOT NULL REFERENCES sync_runs(id),
    observed_at TIMESTAMPTZ NOT NULL,
    change TEXT NOT NULL CHECK (change IN ('new', 'changed', 'unchanged', 'expired')),
    PRIMARY KEY (opportunity_id, sync_run_id)
);

REVOKE ALL ON TABLE opportunity_scans, opportunities, opportunity_events FROM anon, authenticated;
ALTER TABLE opportunity_scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_events ENABLE ROW LEVEL SECURITY;
