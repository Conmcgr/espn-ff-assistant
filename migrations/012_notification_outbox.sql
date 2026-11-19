CREATE TABLE notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id UUID NOT NULL REFERENCES recommendations(id),
    user_id UUID NOT NULL REFERENCES users(id),
    league_season_id UUID NOT NULL REFERENCES league_seasons(id),
    scoring_period INTEGER NOT NULL,
    dedupe_key TEXT NOT NULL,
    urgency TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('send', 'batch')),
    reason TEXT NOT NULL,
    body TEXT NOT NULL,
    deliver_after TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    sent_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    last_error TEXT,
    reply_code TEXT NOT NULL DEFAULT upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 10)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (deliver_after < expires_at),
    UNIQUE (user_id, league_season_id, dedupe_key, urgency)
);
CREATE UNIQUE INDEX notification_reply_open ON notification_outbox(user_id, reply_code)
    WHERE status IN ('pending', 'sending', 'unknown', 'sent');
CREATE INDEX notification_due ON notification_outbox(deliver_after) WHERE status = 'pending';
REVOKE ALL ON TABLE notification_outbox FROM anon, authenticated;
ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;
