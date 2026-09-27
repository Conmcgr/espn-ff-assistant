-- 007: correct matchup unique key to (league_season_id, provider_matchup_id).
--
-- ESPN matchup IDs are sequential per season (0, 1, 2, …), not globally
-- unique. Migration 004's bare unique index on provider_matchup_id treated
-- id=0 from 2015 and id=0 from 2020 as the same row, causing most seasons
-- to silently overwrite each other. The correct key is the compound of
-- season + matchup id.

-- Drop the incorrect global unique index from migration 004.
DROP INDEX IF EXISTS matchups_provider_matchup_id_idx;

-- Add the correct compound unique constraint.
CREATE UNIQUE INDEX IF NOT EXISTS matchups_season_provider_matchup_id_idx
    ON matchups (league_season_id, provider_matchup_id)
    WHERE provider_matchup_id IS NOT NULL;

-- Clear any corrupt matchup rows from previous loads so the next loader
-- run starts with a clean matchups table.
DELETE FROM matchups;
