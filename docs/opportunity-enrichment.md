# Opportunity enrichment

Live ledger opportunities can carry external context: depth chart, injury
report, recent offensive snap share, and cited player news. Enrichment never
creates, changes, or blocks an opportunity; a missing source leaves the
opportunity with fewer signals and a recorded source state.

```sh
uv run python scripts/enrich_opportunities.py --league <id> --season 2026 --week 11
uv run python scripts/enrich_opportunities.py --league <id> --season 2026 --week 11 --record
```

The default run is read-only. `--record` stores one `enrichment_runs` row
(per-source state, retrieval time, `Last-Modified`, matched/unmatched counts)
and one `opportunity_signals` row per signal (migration 016).

## Sources

All sources are free and need no key. Requests use a credential-free session
(`trust_env=False`), so no ESPN cookie, netrc entry, or proxy credential is sent.

| Signal | Source | Fetch | `as_of` | Fresh when |
|---|---|---|---|---|
| depth_chart | nflverse `depth_charts_<season>.csv` | once per run | snapshot `dt` | snapshot within 8 days |
| injury | nflverse `injuries_<season>.csv` | once per run | file `Last-Modified` | report is for the current week |
| usage | nflverse `snap_counts_<season>.csv` | once per run | file `Last-Modified` | last game within 2 weeks |
| news | ESPN fantasy news feed | per shortlisted player | item `published` | always (7-day window) |

News is fetched only for the soonest-expiring players (`--news-limit`, default
5; `--no-news` disables it) and keeps the 3 most recent player-tagged items
with their citation URL.

## Point-in-time and ID rules

- Players match by ESPN ID only: directly for depth charts and news, through
  the nflverse `players.csv` crosswalk (`gsis_id`, `pfr_id`) for injuries and
  snaps. Names are never matched; D/ST entries have no external match.
- Depth-chart snapshots and news items after `observed_at` are ignored.
- Weekly injury and snap files have no per-row timestamp, so they are used only
  when their `Last-Modified` is at or before `observed_at`; otherwise the source
  state is `after_observation` (or `undated` without the header). Replaying an
  old observation therefore gets no injury or usage signals.
- Failed, empty, and disabled sources are recorded as such and add no signals.

Licensed expert consensus or a paid news feed would be another `Sources`
implementation; none is wired in, and adding one needs owner approval.
