CREATE TABLE schedule_fire_log (
    id           BIGSERIAL PRIMARY KEY,
    schedule_id  BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    slot_fire_at TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, slot_fire_at)
);
