-- Contract the delivery watchdog's durable retry state (expand-contract: the code
-- removals shipped first). The recovery loops, the per-inbound dispatch cap /
-- backoff / poison and the stall-alert dedup are gone; no query reads or writes
-- any of the five objects below on origin/main.
--
--   * delivery_watchdog_alerted   - stall-alert dedup of inbound ids already
--                                   WARNINGed while pending;
--   * delivery_watchdog_attempts  - per (loop kind, agent) cooldown clock and
--                                   wake-suppression escalation counters;
--   * inbound_messages.dispatch_count / last_dispatch_at / poisoned_at - the
--                                   per-inbound dispatch cap, backoff clock and
--                                   poison marker.
--
-- Idempotent: every statement is IF EXISTS, so it replays on a baseline that
-- already lacks the objects.

DROP TABLE IF EXISTS delivery_watchdog_alerted;
DROP TABLE IF EXISTS delivery_watchdog_attempts;
ALTER TABLE inbound_messages
    DROP COLUMN IF EXISTS dispatch_count,
    DROP COLUMN IF EXISTS last_dispatch_at,
    DROP COLUMN IF EXISTS poisoned_at;
