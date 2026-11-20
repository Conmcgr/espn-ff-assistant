CREATE TABLE enrichment_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    observed_at TIMESTAMPTZ NOT NULL,
    sources JSONB NOT NULL
);

CREATE TABLE opportunity_signals (
    run_id UUID NOT NULL REFERENCES enrichment_runs(id),
    opportunity_id UUID NOT NULL REFERENCES opportunities(id),
    kind TEXT NOT NULL CHECK (kind IN ('depth_chart', 'injury', 'usage', 'news')),
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    freshness TEXT NOT NULL CHECK (freshness IN ('fresh', 'stale')),
    attrs JSONB NOT NULL,
    citation_url TEXT,
    PRIMARY KEY (run_id, opportunity_id, kind, source_ref)
);

CREATE INDEX opportunity_signals_opportunity ON opportunity_signals (opportunity_id);

REVOKE ALL ON TABLE enrichment_runs, opportunity_signals FROM anon, authenticated;
ALTER TABLE enrichment_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_signals ENABLE ROW LEVEL SECURITY;
