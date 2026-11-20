# Opportunity ledger

The scoped worker records a league-season ledger in its recommendation transaction,
after validating the loaded roster. Player status is pinned to the loaded sync.
All opportunity comparisons use the input observation timestamp, not the clock.

Executed DROP items qualify only while the player is in the current pool. Drops
without a process timestamp are ignored; future-dated drops are ignored. Expiry
is seven days after processing or waiver clearance, whichever is later. Starter
drop expiry never moves earlier if a later provider status omits waiver clearance.
OUT (including INJURY_RESERVE) opportunities exclude bench/IR slots and byes,
require a known kickoff, and expire at kickoff or when qualification ends.

Keys are terminal: once expired, the same drop or team/player/week cannot become
fresh again. Changes to attributes or expiry are recorded as changed. Source view,
first sync, first observation, and key remain stable. Events store the change type;
attributes on the opportunity represent its latest observation, not an audit of
every historical attribute value.

`opportunity_scans` records the high-water mark even for scans with no candidates.
Same/older observations and previously processed sync IDs return `skipped_replay`
without events. Each recording transaction writes a per-league-season ledger-state
row before reading the ledger. Concurrent repeatable-read snapshots that miss a
committed ledger update fail serialization and roll back rather than writing a
stale diff. A later worker run can retry from a fresh snapshot.

## Synthetic worker demo

With `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database with `anon`
and `authenticated` roles:

```sh
uv run pytest -q -s tests/test_worker.py::test_opportunity_demo
```

This invokes the real worker CLI, archive, loader, repository, and recommendation
engines. Only ESPN fetching is simulated. It prints new, unchanged, replay-skipped,
changed, and expired ledger events. Each invocation uses an isolated synthetic
league so the demo can run repeatedly against the same disposable database.
