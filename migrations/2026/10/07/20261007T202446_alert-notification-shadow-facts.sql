ALTER TABLE alerts ADD COLUMN notification_revision BIGINT NOT NULL DEFAULT 0
    CHECK (notification_revision >= 0);
COMMENT ON COLUMN alerts.notification_revision IS
    'Current shadow notification transition revision, not provider acceptance or completion.';

CREATE TABLE alert_notification_groups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('unresolved','resolved')),
    alertname TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('zh','en')),
    render_version TEXT NOT NULL CHECK (render_version = 'alert-group-v1'),
    text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'shadow' CHECK (origin = 'shadow'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE alert_notification_groups IS
    'Immutable shadow ingest groups; never queued, accepted or sent. Legacy may have delivered them: no automatic historical dispatch or expiry.';

CREATE TABLE alert_notification_members (
    alert_id BIGINT NOT NULL CHECK (alert_id > 0),
    notification_revision BIGINT NOT NULL CHECK (notification_revision > 0),
    group_id BIGINT NOT NULL REFERENCES alert_notification_groups(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    reason TEXT NOT NULL CHECK (reason IN ('fresh_firing','refire','escalation','resolution','legacy_unconfirmed')),
    fingerprint TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    source JSONB NOT NULL,
    PRIMARY KEY (alert_id, notification_revision),
    UNIQUE (group_id, ordinal)
);
COMMENT ON TABLE alert_notification_members IS
    'One immutable group membership per shadow instance transition; snapshots survive alert retention and are not delivery receipts.';
