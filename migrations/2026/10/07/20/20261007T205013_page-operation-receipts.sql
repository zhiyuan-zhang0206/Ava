CREATE TABLE page_operation_receipts (
    operation_key text PRIMARY KEY,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    acceptance jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now()
);
