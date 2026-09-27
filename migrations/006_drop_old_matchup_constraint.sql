-- 006: drop the old composite unique constraint on matchups.
--
-- Migration 003 created matchups with UNIQUE (league_season_id, matchup_period,
-- home_provider_team_id, away_provider_team_id). Migration 004 added a partial
-- unique index on provider_matchup_id. The old constraint prevents the loader
-- from inserting the same matchup when it appears in multiple scoring-period
-- mBoxscore responses. Now that every row has a stable provider_matchup_id,
-- the old constraint is redundant and must be removed.
--
-- Any partial matchup rows from a previous failed load will be handled by the
-- loader's ON CONFLICT (provider_matchup_id) upsert on the next run.

DO $$
DECLARE
    conname_var TEXT;
BEGIN
    SELECT conname INTO conname_var
    FROM pg_constraint
    WHERE conrelid = 'matchups'::regclass
      AND contype = 'u'
      AND conname LIKE '%matchup_period%home%';
    IF conname_var IS NOT NULL THEN
        EXECUTE format('ALTER TABLE matchups DROP CONSTRAINT %I', conname_var);
    END IF;
END $$;
