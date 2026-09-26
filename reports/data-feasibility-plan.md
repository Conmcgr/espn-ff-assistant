# ESPN data-feasibility plan

**Status: COMPLETE** — All five questions answered. Spike closed 2026-09-26.
Manual spot-checks completed 2026-09-26. See `reports/verification.local.md`
(Git-ignored) for detailed records.

## Questions answered

1. **How far back is manager identity reliable?**
   Reliable from 2018 onward using ESPN member IDs from `mTeam`. Pre-2018
   seasons have teams and rosters but `mTransactions2` is absent; cross-season
   identity joins must carry explicit uncertainty.

2. **Are completed adds and drops recoverable?**
   Yes, from 2018 onward. The 2018–2026 archive contains 1,697 waiver claims
   and 1,201 free-agent moves with team, member, player, and date fields. No
   transaction records are available for 2013–2017.

3. **Are successful FAAB amounts recoverable?**
   Yes, from 2018 onward. `bidAmount` is present in waiver-claim transaction
   records. Failed-claim statuses are also present where the bid was submitted.

4. **Are completed trades recoverable with all assets and participants?**
   Partially. 30 completed-trade events and 268 trade proposals are present from
   2018–2026. Individual trade legs (player moves) are in `transaction_items`.
   Pre-2018 trades are not recoverable from the transaction feed.

5. **Can weekly roster state be reconstructed?**
   Yes, for all seasons 2013–2026. `mRoster` per scoring period returned full
   roster payloads for every scanned week.

## Feasibility boundary

Use **2018 onward** as the primary transaction and manager-behavior modeling
window. Seasons 2013–2017 contribute roster, draft, settings, and standings
context only.

## Known gaps and constraints

- `mSchedule` top-level view returns no schedule array; matchups must be read
  from per-week `mBoxscore` payloads (which contain the full season schedule).
- Cross-season manager identity requires stable member IDs or manual
  confirmation; some uncertainty is irreducible.
- The ESPN activity/communication feed returns HTTP 404 for 2019–2025; it is
  optional current-season enrichment only.
- ESPN integration uses unofficial endpoints and private session credentials.
  The league provider must remain architecturally replaceable.
- Pre-2018 trade records are absent; do not backfill or invent them.

## Coverage semantics

- `missing`: expected response key is absent
- `empty`: key exists with zero records
- `partial`: records exist but are incomplete or bounded
- `present`: records exist but have not been externally verified
- `verified`: representative records match ESPN UI
- `error`: request, authentication, or parsing failed

## Execution history

1. Validated local credentials without logging values.
2. Proved 2026 access: status, settings, teams/owners, draft, rosters, matchups.
3. Saved raw responses under `data/raw/` (Git-ignored).
4. Scanned 2013–2026; all seasons returned status and roster payloads.
5. Normalized `mTransactions2` from 2018–2026: 5,772 transactions.
6. Produced `reports/coverage.csv`.
7. Verified representative records against ESPN UI (2026-09-26).
8. Classified all five core questions — see above.
9. Loaded scan `20260923T011957Z` into Supabase.
