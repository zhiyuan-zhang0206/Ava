# Cluster security posture — open debt (2026-08-02 assessment)

> Assessment: 2026-08-02, task #661 (full report:
> internal agent-memory note).
> This page is the durable home for the **open** items from that assessment —
> the previous home was a gitignored agent memory note
> (`memory/cluster-security-posture-2026-08-02.md`), which now points here
> (facts belong on their axis; a gitignored note is one `git add -f` away from
> shipping with the repo).

## Trust model (context, not debt)

`AVA_CLUSTER_SECRET` (43-char high-entropy) authorizes the gateway API and ops
RPC only. The data plane has separate file-only gateway administrator credentials
(`AVA_DB_ADMIN_PASSWORD`, `AVA_REDIS_ADMIN_PASSWORD`) and independent runner
runtime credentials (`AVA_RUNNER_DB_PASSWORD`, `AVA_REDIS_PASSWORD`), so a
runner bearer cannot become a data-plane administrator. No per-agent identity
inside the cluster; every agent process environment carries the bearer + all 11
provider keys. Postgres and PgBouncer bind loopback + the machine's reachable
private-network address only; authenticated Linux Redis does the same directly,
while macOS Redis remains loopback-only and off-box inbound uses its relay bridge;
`pg_hba` allows the whole private network (100.64.0.0/10)
with scram; unix-socket local trust (OS user is the trust root).

## User rulings (do NOT "fix")

- **Prompt-injection scan off is deliberate**: `AVA_SECURITY_SCAN_ENABLED=false`
  — user ruling (little web browsing, low injection worry). Not a config drift.

## Open debt

1. **`ava.ui` page servers bind `("", port)` (0.0.0.0) unauthenticated** —
   pure static content, no auth, reachable from LAN/private network (ALF has a
   rule for it). Fix direction pending user decision: bind loopback / route via
   the gateway reverse proxy / private-network-only.
2. **World-readable `~/.ava` files** — `logs/`, `workspaces/` (710 dirs),
   `memory/` are 755/644, containing health/financial data; `.env` (600) and
   `secrets/` (700) are correct.

## Resolved since the assessment

- Login rate limiting resolved (2026-09-01): `POST /api/auth/login` uses the
  per-IP `LoginRateLimiter` in [`shared/rate_limit.py`](../../shared/rate_limit.py);
  [`gateway/routers/auth.py`](../../gateway/routers/auth.py) returns 429 with
  `Retry-After` during lockout, and
  [`tests/gateway/test_login_endpoint.py`](../../tests/gateway/test_login_endpoint.py)
  pins the threshold, reset, expiry, and IP-isolation contract.
- macOS ALF manifest drift is reconciled by `ava converge`: manifest globs cover
  the current inbound binaries, direct `socketfilterfw` mutation was empirically
  verified on the macmini running macOS 15.3.1, and elevation on other platforms
  remains a bounded `sudo -n` / manual-command fallback (2026-08-24, task #1531).
- Cloudflare tunnel deprecated (2026-08-02 ruling): the ava-prod user
  LaunchAgent is disabled; the root LaunchDaemon
  `com.cloudflare.cloudflared` (token tunnel) was scheduled for
  `sudo launchctl bootout system/com.cloudflare.cloudflared` + plist removal.
- Kapture removed from the machine; the :61822 listener found 2026-08-02 was an
  orphan MCP process from a 7/30 Claude Code session (review #986), killed.
