CREATE TABLE IF NOT EXISTS roster_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    scoring_period INTEGER NOT NULL,
    provider_team_id BIGINT NOT NULL,
    source_payload_id UUID,
    UNIQUE (league_season_id, scoring_period, provider_team_id)
);

CREATE TABLE IF NOT EXISTS roster_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    roster_snapshot_id UUID NOT NULL REFERENCES roster_snapshots(id),
    provider_player_id BIGINT NOT NULL,
    lineup_slot_id INTEGER,
    acquisition_type TEXT,
    applied_stat_total NUMERIC,
    source_payload_id UUID,
    UNIQUE (roster_snapshot_id, provider_player_id, lineup_slot_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    matchup_period INTEGER NOT NULL,
    home_provider_team_id BIGINT,
    away_provider_team_id BIGINT,
    home_score NUMERIC,
    away_score NUMERIC,
    source_payload_id UUID,
    UNIQUE (league_season_id, matchup_period, home_provider_team_id, away_provider_team_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    overall_pick INTEGER,
    round INTEGER,
    round_pick INTEGER,
    provider_team_id BIGINT,
    provider_player_id BIGINT,
    bid_amount INTEGER,
    source_payload_id UUID,
    UNIQUE (league_season_id, overall_pick)
);

CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    provider_transaction_id TEXT NOT NULL,
    scoring_period INTEGER,
    provider_type TEXT,
    status TEXT,
    category TEXT,
    provider_team_id BIGINT,
    provider_member_id TEXT,
    bid_amount NUMERIC,
    process_date TIMESTAMPTZ,
    proposed_date TIMESTAMPTZ,
    source_payload_id UUID,
    UNIQUE (league_season_id, provider_transaction_id)
);

CREATE TABLE IF NOT EXISTS transaction_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID NOT NULL REFERENCES transactions(id),
    item_type TEXT,
    provider_player_id BIGINT,
    from_provider_team_id BIGINT,
    to_provider_team_id BIGINT,
    source_payload_id UUID
);

CREATE INDEX IF NOT EXISTS roster_snapshots_season_period_idx
    ON roster_snapshots (league_season_id, scoring_period);
CREATE INDEX IF NOT EXISTS roster_entries_snapshot_idx
    ON roster_entries (roster_snapshot_id);
CREATE INDEX IF NOT EXISTS matchups_season_period_idx
    ON matchups (league_season_id, matchup_period);
CREATE INDEX IF NOT EXISTS draft_picks_season_idx
    ON draft_picks (league_season_id);
CREATE INDEX IF NOT EXISTS transactions_season_period_idx
    ON transactions (league_season_id, scoring_period);
CREATE INDEX IF NOT EXISTS transaction_items_transaction_idx
    ON transaction_items (transaction_id);

REVOKE ALL ON TABLE
    roster_snapshots,
    roster_entries,
    matchups,
    draft_picks,
    transactions,
    transaction_items
FROM anon, authenticated;

ALTER TABLE roster_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE roster_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE matchups ENABLE ROW LEVEL SECURITY;
ALTER TABLE draft_picks ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction_items ENABLE ROW LEVEL SECURITY;

