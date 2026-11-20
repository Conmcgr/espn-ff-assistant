CREATE TABLE trigger_firings (
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    trigger_key TEXT NOT NULL,
    fired_at TIMESTAMPTZ NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (league_season_id, trigger_key)
);

REVOKE ALL ON TABLE trigger_firings FROM anon, authenticated;
ALTER TABLE trigger_firings ENABLE ROW LEVEL SECURITY;
