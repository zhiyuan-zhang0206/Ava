-- A lifecycle command must not sit at `done` while `agents_meta.lifecycle_command_id`
-- still points at it. That torn shape blinds every recovery engine at once: boot
-- recovery requires the command still `claimed`, live observation requires a live
-- process identity, and the boot reconcile adopts only unfinished commands — so a
-- resurrection of the affected agent defers forever (instances 6285/200306 and
-- 6089/172352; task #3678).
--
-- The invariant is enforced at COMMIT time by a deferred constraint trigger: every
-- legitimate writer settles the command and clears the pointer in the same
-- transaction (supersede / force-supersede / observe / hosted-force settle), so the
-- only way to carry pointer -> done across a commit is an out-of-band write — e.g. a
-- manual "cleanup" UPDATE that flips the row to done without settling it.
--
-- Order: repair the rows that already violate the invariant first (a set predicate —
-- pointer alive + status 'done' — never hard-coded ids), then install the guard.
-- The trigger only fires on the transition INTO done (UPDATE); a pointer later set
-- onto an already-done row is out of this fence — the TTL reaper's hourly
-- torn-pointer scan (gateway/ttl_reaper.py, telemetry lifecycle_pointer_done_torn)
-- is the bypass detector for that side.
--
-- Repair counts land in the server log via RAISE NOTICE: on the authoring cluster
-- this settled 1 row and cleared 1 pointer (agent 6089, command 172352).
DO $$
DECLARE
    settled INT;
    cleared INT;
BEGIN
    UPDATE inbound_messages i
    SET observed_at = clock_timestamp()
    FROM agents_meta m
    WHERE m.lifecycle_command_id = i.id AND m.id = i.agent_id
      AND i.status = 'done' AND i.applied_at IS NOT NULL AND i.observed_at IS NULL;
    GET DIAGNOSTICS settled = ROW_COUNT;

    UPDATE agents_meta m
    SET lifecycle_command_id = NULL
    FROM inbound_messages i
    WHERE m.lifecycle_command_id = i.id AND m.id = i.agent_id
      AND i.status = 'done';
    GET DIAGNOSTICS cleared = ROW_COUNT;

    RAISE NOTICE 'lifecycle pointer->done repair: settled % unobserved command row(s), cleared % live pointer(s)', settled, cleared;
END $$;

CREATE OR REPLACE FUNCTION reject_inbound_done_with_lifecycle_pointer() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agents_meta m
        WHERE m.lifecycle_command_id = NEW.id AND m.id = NEW.agent_id
    ) THEN
        RAISE EXCEPTION 'inbound % (agent %) cannot reach done while agents_meta.lifecycle_command_id still points at it — settle the command and clear the pointer in the same transaction', NEW.id, NEW.agent_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER inbound_messages_lifecycle_pointer_done_guard
    AFTER UPDATE ON inbound_messages
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.status = 'done' AND OLD.status IS DISTINCT FROM 'done'
          AND NEW.kind IN ('restart', 'terminate'))
    EXECUTE FUNCTION reject_inbound_done_with_lifecycle_pointer();
