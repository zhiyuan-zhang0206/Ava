# Telegram skill removed — the IM Bridge is the only Telegram surface

## Context

The `telegram` skill (`ava_builtins/skills/telegram/`) let any agent push a
one-way message to the owner's phone (and pull replies) by calling
`api.telegram.org` inline with the cluster bot token. It replaced the earlier
framework-registered Telegram service (see `2026-07-22-telegram-out-of-core.md`).

In 2026-08 the IM Bridge (`services/im_bridge/`) became the product frontend:
Telegram / WeChat / Feishu are user-facing channels the bridge owns, with the
bot token living in cluster config, a daemon polling the platform, and a
documented source=user envelope path (the IM user is the web user).

## Decision

The `telegram` skill is **removed entirely**. The IM Bridge is the **only**
surface that talks to Telegram. Agents never call the Bot API directly.

User ruling (2026-08-03): an agent sending the owner a message straight through
the bot API (bypassing the frontend) was confusing — "since we are a frontend, stay
pure frontend — no Telegram skill" (user, translated). This supersedes the earlier position that a
low-frequency push channel belongs at skill altitude.

## Consequences

- `ava_builtins/skills/telegram/` and its OKF doc are deleted; the comms skill
  index no longer lists Telegram.
- Agents reach the owner through `ava.ui.notify` (queue) and the IM Bridge
  (frontend); nothing else.
- The bridge's Telegram adapter (`services/im_bridge/adapters/telegram.py`)
  remains the single implementation of Bot API access.
- The cluster-health probe's owner alert (the last direct Bot API caller,
  `cli/commands/_cluster_health.py:_notify_owner`) was migrated to the bridge's
  health-port `/send` RPC on 2026-08-04 (event-system W11) — the bridge is now
  the only surface that talks to Telegram, full stop.
- The SMS skill (reading verification codes on macOS) is unrelated and stays.
