# Notification outbox contract

The worker's `--outbox` option saves recommendations, evidence and intents in
one transaction. Default shadow runs do not save any of these. No transport is
implemented. Suppressed decisions appear in the JSON audit output, not the
outbox; their engine records remain in recommendations.

## Policy

Only `notify` and `urgent` records may queue. Records more than 15 minutes old,
future-dated records and expired records suppress. Unknown deadlines expire
15 minutes after observation. Lineup intents use the earliest known kickoff in
the scoring period (deliberately conservative, including already-started games).
Explicit aware `payload.expires_at` deadlines are supported by the pure policy;
waivers currently fall back to 15 minutes because a verified waiver deadline is
not normalized. A digest at or after expiry suppresses instead of queuing stale
advice. Thus long overnight batches are often intentionally silent.

Defaults: 22:00–09:00 America/Chicago, two immediate non-urgent sends per local
day. Invalid settings fall back to these defaults. Quiet windows can cross
midnight. Zoneinfo and UTC round-tripping handle DST transitions. Daily counts
include already queued immediate intents, not only delivered messages.

Canonical SHA-256 keys cover user, league season, team, period, kind, and sorted
lineup player/from/to moves or add/drop IDs. Names, projections, summaries and
bids do not change the action identity. The unique database key includes urgency:
one notify intent and one urgent escalation are possible per canonical action,
never two intents of the same tier. Retries retain the key even after expiry or
failure. Worker ticks serialize policy accounting with a user/league advisory
transaction lock; database uniqueness and `ON CONFLICT DO NOTHING` additionally
protect independent enqueue callers. All worker outbox writers must take this
lock to share cap accounting.

## M1 sender boundary

`Repository.due_notifications(now)` returns only pending intents with
`deliver_after <= now < expires_at`. This is a **read**, not a claim. M1 must
atomically claim before sending, recheck expiry, and transition
`pending → sending → sent | failed | unknown`. `unknown` holds ambiguous provider
results for reconciliation; do not blindly retry. Schema includes `attempts`,
`provider_message_id`, `last_error`, and `sent_at` (initially null). Enqueuing an
urgent escalation marks an older still-pending notify `failed` with
`last_error='superseded_by_urgent'`; never retry that terminal intent. An already
sending/unknown notify is left for M1 to reconcile safely.

## M2 reply boundary

`reply_code` is a random ten-character uppercase code, unique per user while
pending, sending, unknown or sent. It maps to the actual `recommendation_id`;
never attach replies to "latest recommendation". M2 must verify the replying
user, resolve the code, and write feedback through the repository. Rendering the
code in provider messages belongs to M1. No phone numbers or credentials are
stored here. RLS is enabled and anon/authenticated access is revoked.
