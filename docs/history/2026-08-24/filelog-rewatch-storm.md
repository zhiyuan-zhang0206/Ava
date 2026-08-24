# Filelog re-watch storm

Collector 0.155.0 fingerprints files from their leading content. Agent main
stdout files shared the same telemetry banner prefix, so a broad
`$AVA_HOME/logs/*.out.log` receiver repeatedly treated collision peers as new
files. With archive metadata disabled and no local transcript retention, the
collision set produced a sustained watch storm and unbounded disk growth.

The collector now uses two disjoint raw-output receivers: agent shell
transcripts are admitted by their specific name shape, while service stdout is
admitted by the broad glob only after excluding every agent file and the
collector itself. Both receivers poll every 10 seconds, retain 50 generations
of EOF metadata, and cap concurrent discovery at 200 files. The pty-host startup
path independently prunes top-level `*.out.log` files older than seven days,
with one locked scan per machine per day.

The earlier single-receiver narrowing from PR #477 was rejected because it
would also remove gateway, daemon, and schedule output from Loki. The dual
receiver design preserves those diagnostic streams while keeping banner-only
agent main stdout out of filelog; structured agent records continue through
OTLP. This change incorporates the final receiver split coordinated with
#3279.

Update: the broad seven-day PTY-startup deletion path was superseded by the
explicit, active-handle-safe policy in [log-retention-cli](log-retention-cli.md).
