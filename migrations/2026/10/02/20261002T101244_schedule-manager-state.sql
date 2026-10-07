-- State of the schedule-manager service that was process-local in the gateway:
-- the crash backoff and launch counter, the sessionless clock and the stall
-- alert, and the queue of API sync requests.
--
--   launch_count / next_launch_at   a launch is claimed with one conditional
--       UPDATE (services/schedule_manager/manager.py::_claim_launch); the count
--       resets after the schedule stays up, and the breaker trips at the ceiling
--   not_live_since / stall_alerted_at   the two-hour no-session alert fires once
--       per outage and survives a restart; a live observation clears both
--   schedule_sync_requests   start / stop / restart / edit / delete leave a row
--       the service consumes (gateway/schedules/session_control.py); no foreign
--       key, a delete asks for the orphaned session to be killed after the row
--       is gone
--
-- IF NOT EXISTS keeps the migration replayable on a baseline that already
-- carries the objects.
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS launch_count INT NOT NULL DEFAULT 0;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS next_launch_at TIMESTAMPTZ;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS not_live_since TIMESTAMPTZ;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS stall_alerted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS schedule_sync_requests (
    schedule_id  BIGINT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE schedule_sync_requests IS
    'Pending API requests for the schedule-manager service to converge one schedule''s session now (kill, relaunch if enabled). The consumer deletes a row after the sync ran, only if requested_at is unchanged.';
