-- 008: flag manager features derived from a co-owned team.
--
-- Waiver, FAAB, churn, and holding features are computed from team activity.
-- When a team has several owners, each owner receives identical values; the
-- flag lets consumers treat the team, not the member, as the acting unit.

ALTER TABLE manager_features
    ADD COLUMN IF NOT EXISTS shared_team BOOLEAN NOT NULL DEFAULT false;
