-- 009: replace ESPN's negative sentinel teamId on transactions.
--
-- Some 2018 transactions carry teamId = -2147483648. The acting team is the
-- destination of the transaction's ADD item; the normalizer now derives it the
-- same way. Rows without an ADD item have no recoverable acting team.

UPDATE transactions t
SET provider_team_id = (
    SELECT ti.to_provider_team_id
    FROM transaction_items ti
    WHERE ti.transaction_id = t.id
      AND ti.item_type = 'ADD'
      AND ti.to_provider_team_id > 0
    ORDER BY ti.id
    LIMIT 1
)
WHERE t.provider_team_id < 0;
