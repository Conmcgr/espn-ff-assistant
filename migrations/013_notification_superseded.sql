ALTER TABLE notification_outbox DROP CONSTRAINT notification_outbox_status_check;
ALTER TABLE notification_outbox ADD CONSTRAINT notification_outbox_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'unknown', 'superseded'));
UPDATE notification_outbox SET status='superseded'
    WHERE status='failed' AND last_error='superseded_by_urgent';
