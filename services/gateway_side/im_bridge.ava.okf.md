---
type: doc
title: IM Bridge — the product frontend
description: "The IM Bridge (`services/im_bridge/`) is the product frontend: every user-facing IM channel (Telegram / WeChat / Feishu) is an adapter the bridge owns — message envelope, command routing, per-chat switch state, SSE subscription push of agent dialog."
tags: []
---

# IM Bridge — the product frontend

## What it is
The IM Bridge daemon runs every configured IM channel adapter (Telegram / WeChat / Feishu — one adapter per channel, sharing the bridge core). Since 2026-08 the bridge **is the product frontend** (user ruling: IM is the only frontend; the Telegram skill was removed — see `decisions/2026-08-03-telegram-skill-removed.md`): user-facing channels are not a side feature but the primary surface.

**Role affiliation**: gateway side — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, `requires_db=True` (R3 door ④: notice_bridge reads and lazily expires `agent_notices` directly — previously declared False with the drift noted in-okf; the spec now matches). Kept alive by `services/healthchecks/im_bridge.py` (gateway watchdog).

## Core Responsibilities
- **Adapters** (`services/im_bridge/adapters/<channel>.py`): each channel is one adapter sharing the core — envelope, command routing, per-channel session state. An adapter whose module is missing or credentials unset logs "skipped"; the daemon keeps serving the others. `AVA_IM_DISABLED_ADAPTERS` disables channels. Feishu inbound is dual-path: `im.message.receive_v1` WS events plus a ListMessage polling fallback (`AVA_FEISHU_POLL_INTERVAL_SECONDS` / `AVA_FEISHU_POLL_CHAT_ID`) for platforms that never push events — a shared seen-set keeps the two paths idempotent.
- **Dialog push**: cold loads read `GET /timeline`, live updates come from `timeline_snapshot` SSE events; both render through the same dialog filter (`inbound_chat` with `source='user'` + `agent_chat`), so a switch's "recent 5 messages" and the pushed tail agree item-for-item. `_PUSH_LIMIT` splits long messages.
- **Command set**: `/list` / `/switch` / `/status` / `/spawn` / `/commands` / `/help`, plus `/notice` (notice queue) — see `core.py`; per-chat switch state persists (`_switch_state_path()`) across daemon restarts.
- **Spawn menu** (`spawn_menu.py`): interactive agent-spawn flow from the chat (SpawnDraft / SpawnMenuMixin).
- **Push watchdog** (`push_watchdog.py`): WeChat context-token expiry watchdog (Task #829) — alerts the user via other channels and gives recovery hints when the WeChat push link dies; **dormant in production** since weixin is disabled (2026-08-06). Not an SSE re-pusher.
- **Ops-alert fan-out**: `IMBridgeCore.notify_user` sends ops alerts (P0/P1 by default) to the owner chat — see [[gateway/routers/alerts.ava.okf.md]].

## Key Dependencies
- [[gateway/routers/routers.ava.okf.md]] — the gateway REST client + SSE subscription the bridge consumes
- [[gateway/routers/alerts.ava.okf.md]] — IM notification fan-out for ops alerts

## Entry Points
- `services/im_bridge/daemon.py` — `.venv/bin/python -m services.im_bridge.daemon`
- `services/im_bridge/core.py` — shared envelope / command routing / SSE push (`IMBridgeCore`)
- Watchdog keeps alive via `services/healthchecks/im_bridge.py`

## Notes
- Bridge ↔ agent dialog is human messages in, agent text out — tool execution / reasoning / other agents' messages are never pushed to IM
