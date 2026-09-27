-- 011: user identity per season, preferences, recommendations, evidence,
-- feedback, and outcomes. Every engine run is persisted, including NO_ACTION.

CREATE TABLE IF NOT EXISTS user_teams (
    user_id UUID NOT NULL REFERENCES users(id),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_team_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, league_season_id)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    league_id UUID NOT NULL REFERENCES leagues(id),
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    weight NUMERIC NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL,               -- 'onboarding' | 'learned' | 'explicit'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, league_id, key)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_team_id BIGINT NOT NULL,
    kind TEXT NOT NULL,                 -- 'lineup' | 'waiver'
    scoring_period INTEGER NOT NULL,
    as_of_ts TIMESTAMPTZ NOT NULL,
    decision TEXT NOT NULL,             -- 'urgent' | 'notify' | 'info' | 'suppressed' | 'no_action'
    summary TEXT NOT NULL,
    payload JSONB NOT NULL,
    alternatives JSONB,
    confidence TEXT,
    engine_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recommendations_lookup_idx
    ON recommendations (user_id, league_season_id, scoring_period, kind);

CREATE TABLE IF NOT EXISTS recommendation_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id UUID NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    source TEXT
);

CREATE INDEX IF NOT EXISTS recommendation_evidence_rec_idx
    ON recommendation_evidence (recommendation_id);

CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id UUID NOT NULL REFERENCES recommendations(id),
    action TEXT NOT NULL CHECK (action IN ('accepted', 'rejected', 'modified', 'ignored', 'impossible')),
    reason TEXT CHECK (reason IN ('too_much_faab', 'dont_believe_player', 'prefer_current', 'other')),
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    recommendation_id UUID PRIMARY KEY REFERENCES recommendations(id),
    observed_action TEXT,
    recommended_points NUMERIC,
    actual_points NUMERIC,
    baseline_points NUMERIC,
    evaluated_through_period INTEGER,
    detail JSONB,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON TABLE user_teams, user_preferences, recommendations, recommendation_evidence,
    recommendation_feedback, recommendation_outcomes FROM anon, authenticated;
ALTER TABLE user_teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_outcomes ENABLE ROW LEVEL SECURITY;
