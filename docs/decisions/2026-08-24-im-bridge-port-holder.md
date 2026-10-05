# IM Bridge port-holder identity veto

Three duplicate IM Bridge incidents showed that liveness staleness is not a
safe respawn trigger for this daemon. Its work loop can legitimately block on a
long Telegram poll while its independent health server continues answering;
spawning during that interval creates a second process that cannot bind the
health port but can still race the first process for the bot token.

The IM Bridge watchdog now gives socket ownership precedence over liveness and
pidfile agreement. After the shared probe returns a respawnable failure, it
re-reads the same `/healthz`: matching `name="im_bridge"` and this unit's `home`
prove that our daemon holds the port, even for HTTP 503 or a different payload
pid. That state logs the holder pid and `stale_for` at WARNING and suppresses
respawn. An unreadable or mismatched holder preserves the existing verdict and
action.

Changing `shared.daemon_health.probe_daemon` was rejected because liveness
staleness remains valid death evidence for the other daemons. The extra read is
im_bridge-only and reuses the shared five-second probe timeout and body bound,
keeping the full watchdog check within its 60-second round.
