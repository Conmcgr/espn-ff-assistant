-- Private raw ESPN payload archive for hosts without a private bucket
-- (RAW_ARCHIVE_URL=db:///<prefix>). Objects are gzipped by the store.
CREATE TABLE raw_objects (
    key TEXT PRIMARY KEY,
    data BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON TABLE raw_objects FROM anon, authenticated;
ALTER TABLE raw_objects ENABLE ROW LEVEL SECURITY;
