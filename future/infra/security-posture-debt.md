# Cluster security posture — open debt (2026-08-02 assessment)

> Assessment: 2026-08-02, task #661 (full report:
> internal agent-memory note).
> This page is the durable home for the **open** items from that assessment —
> the previous home was a gitignored agent memory note
> (`memory/cluster-security-posture-2026-08-02.md`), which now points here
> (facts belong on their axis; a gitignored note is one `git add -f` away from
> shipping with the repo).

## Trust model (context, not debt)

One `AVA_CLUSTER_SECRET` (43-char high-entropy) = all trust: gateway API bearer +
ops RPC + pg scram + redis requirepass. No per-agent identity inside the cluster;
every agent process env carries the secret + all 11 provider keys. Data plane:
pg/redis bind loopback + the machine's Tailscale IP only; `pg_hba` allows the
whole tailnet (100.64.0.0/10) with scram; unix-socket local trust (OS user is
the trust root).

## User rulings (do NOT "fix")

- **Prompt-injection scan off is deliberate**: `AVA_SECURITY_SCAN_ENABLED=false`
  — user ruling (little web browsing, low injection worry). Not a config drift.

## Open debt

1. **POST /api/auth/login has no rate limit** — user-classified as a bug, wants
   it fixed. No owner yet.
2. **`ava.ui` page servers bind `("", port)` (0.0.0.0) unauthenticated** —
   pure static content, no auth, reachable from LAN/tailnet (ALF has a rule for
   it). Fix direction pending user decision: bind loopback / route via the
   gateway reverse proxy / tailnet-only.
3. **macOS ALF firewall rules stale** — 9/17 manifest binaries lack rules (uv
   3.11.14/3.12.11, Chrome Helper, etc.), 5 obsolete rules; `/etc/sudoers.d/
   ava-firewall` not installed, so the auto-repair path is inert.
4. **World-readable `~/.ava` files** — `logs/`, `workspaces/` (710 dirs),
   `memory/` are 755/644, containing health/financial data; `.env` (600) and
   `secrets/` (700) are correct.

## Resolved since the assessment

- Cloudflare tunnel deprecated (2026-08-02 ruling): the ava-prod user
  LaunchAgent is disabled; the root LaunchDaemon
  `com.cloudflare.cloudflared` (token tunnel) was scheduled for
  `sudo launchctl bootout system/com.cloudflare.cloudflared` + plist removal.
- Kapture removed from the machine; the :61822 listener found 2026-08-02 was an
  orphan MCP process from a 7/30 Claude Code session (review #986), killed.
