-- Domain receipts never expire into fresh notice effects.
CREATE TABLE notice_operation_receipts (
    path TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK (char_length(operation_key) BETWEEN 1 AND 128),
    request JSONB NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (path, operation_key)
);
