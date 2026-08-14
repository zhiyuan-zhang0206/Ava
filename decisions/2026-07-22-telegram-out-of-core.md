# Telegram out of core — it is a skill, not a framework service

> **Superseded 2026-08-03** by `2026-08-03-telegram-skill-removed.md` — the
> skill itself is removed; the IM Bridge is the only Telegram surface.

## Context

Telegram was a framework-registered service. It had a `ServiceSpec` in the
single-source `ops/spec.py:build_services()` roster, so `ava start`, the watchdog
keepalive loop, and `ava status` all knew about a `telegram` session; a
long-running daemon (`python -m ui.telegram`) subscribed to a redis channel
(`ava:channels:telegram:send`); a gateway route (`POST /api/channels/telegram/send`)
published to that channel; and a healthcheck (`services/healthchecks/telegram.py`)
respawned the daemon. The `telegram` skill drove all of this by POSTing to the
gateway route.

But look at what the daemon actually did: it delivered a one-way text push to the
owner's phone. No inbound, no commands, no replies, no streaming — the module
docstring said so explicitly. The whole apparatus — a registered service, a
persistent redis subscriber, a gateway endpoint, a healthcheck, a reserved port
slot — existed to move a single `sendMessage` call off the caller and onto a
daemon.

This is the point where Ava and OpenClaw diverge. OpenClaw makes an Instant
Messenger the application surface: the IM is expected to cover the whole product,
inbound and outbound. That framing cannot express a multi-agent interface, and it
pulls the messenger into the core as a first-class service. Ava's position is the
opposite: Discord MCP, Twitter MCP, WeChat, Telegram are **tools an agent reaches
for**, not framework-provided channels. A tool the agent uses does not belong in
the service roster, the watchdog, or the gateway's route table.

## Decision

**Delete the telegram framework surface entirely. The two things that send a
telegram — the skill and the cluster-health owner-alert — POST directly to the
Telegram Bot API (`https://api.telegram.org/bot<token>/sendMessage`).**

- The `telegram` skill (`skills/telegram/SKILL.md`) reads `AVA_TELEGRAM_BOT_TOKEN`
  / `AVA_TELEGRAM_OWNER_ID` from `settings.telegram` inside `execute_code` and
  calls the Bot API itself. No gateway endpoint, no redis channel, no daemon.
- `cli/commands/_cluster_health.py:_notify_owner` does the same inline.
- Removed: the `ui/telegram` daemon, `scripts/start_telegram.py`, the healthcheck,
  the `ServiceSpec` + `_gate_reason` branch, the gateway route + schema, the
  `channels_telegram_pubsub` channel (config / agent-context / bootstrap env), the
  telegram service config (pidfile / health_url / health_port), the cluster port
  slots, the daemon_health port, and the `python-telegram-bot` dependency.
- Kept: the two config values (`AVA_TELEGRAM_BOT_TOKEN` / `AVA_TELEGRAM_OWNER_ID`),
  now owned by the `agent-runner` capability, since agent code is the primary
  reader. They are cluster-pinned, so every runner already has the token.

Feasibility was checked first: an agent's `execute_code` can read the token
(cluster-pinned config is bootstrapped to every runner) and reach the public
internet (egress is open; `ava.web` already dials Brave directly, other skills hit
third-party APIs directly). The bot's placement "on the gateway" was an
architectural choice, never a capability constraint.

## Alternatives rejected

**Keep the daemon, move its ownership to the skill (a skill-owned subscriber
server).** This was the first framing — "if Telegram wants a server, the skill
should start it." But a push-only channel needs no persistent process at all, and
a skill has no framework-level lifecycle to keep a daemon alive: an agent invokes
a skill during a turn; it cannot hold a subscriber across restarts. So a
skill-owned daemon would lose the watchdog keepalive the core service had — and
the owner-alert, which depends on someone consuming the channel, would silently
stop the moment that daemon died. This trades a clean deletion for a reliability
regression. Rejected.

**Keep a thin gateway route that calls `bot.send_message` synchronously** (drop
the daemon + redis, keep the endpoint). This removes the keepalive problem but
keeps a `/api/channels/telegram/send` route in the gateway — telegram is still
framework-provided, which is exactly the thing the decision rejects. A gateway
route is core. Rejected.

**Route the owner-alert through the gateway.** The cluster-health probe must reach
the owner precisely when the cluster is unhealthy — possibly when the gateway
itself is the thing that is down. A direct Bot API call keeps the alert
independent of the gateway (as the old redis path was) while also dropping its
dependency on redis + a live subscriber. Going through the gateway would couple
the down-alert to a component that may be down.

## Consequences

- **Telegram has zero core surface.** No service, no route, no channel, no daemon,
  no reserved port, no healthcheck. It sits with Discord/Twitter/WeChat as an
  agent tool. This is the "small core, minimal" principle applied literally.
- **The owner-alert is more robust, not less.** It previously required redis + the
  telegram daemon to both be up — the two things least reliable during an
  incident. A direct Bot API call needs only outbound HTTPS from the gateway host.
- **`cli/commands/_cluster_health.py` is the one core caller that still sends a
  telegram**, inlined as ~5 lines of `httpx`. This is the framework monitoring
  itself and using telegram as a raw notification transport — not registering
  telegram as a service. It is the deliberate exception, called out here so a
  future reader does not mistake it for residue of the old service.
- **Runtime-seamless.** The token is unchanged and already present on every runner
  (cluster-pinned), egress is open, and the probe runs on the gateway host. After
  a rollout the skill push and the owner-alert both work over the same token — no
  re-setup. The old daemon simply stops being respawned.
- **A dev worktree still must not fire real owner notifications.** The token stays
  out of the seed allowlist (`SEED_ENV_KEYS`), so a seeded worktree does not
  inherit it — the same guard as before, kept for the new reason (a dev cluster
  must not push to the prod owner's phone).
