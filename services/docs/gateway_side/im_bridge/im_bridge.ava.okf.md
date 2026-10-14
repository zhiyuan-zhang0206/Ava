---
type: doc
title: IM Bridge — the product frontend
description: "The IM Bridge (`services/entrypoints/im_bridge/`) is the product frontend: every user-facing IM channel (Telegram / WeChat / Feishu) is an adapter the bridge owns — message envelope, command routing, per-chat switch state, SSE subscription push of agent dialog."
tags: []
---

# IM Bridge — the product frontend

## What it is
The IM Bridge daemon runs every configured IM channel adapter (Telegram / WeChat / Feishu — one adapter per channel, sharing the bridge core). Since 2026-08 the bridge **is the product frontend** (user ruling: IM is the only frontend; the Telegram skill was removed — see `docs/decisions/runtime/processes/shutdown/2026-08-03-telegram-skill-removed.md`): user-facing channels are not a side feature but the primary surface.

**Role affiliation**: gateway side. `supervision/roster/__init__.py` declares `GATEWAY` capabilities and `requires_db=True`: notice polling reads `agent_notices` directly. The root supervisor monitors the roster's `/healthz` identity probe.

## Core Responsibilities
- **Adapters** (`services/entrypoints/im_bridge/adapters/<channel>.py`): each channel is one adapter sharing the core — envelope, command routing, per-channel session state. Unset credentials keep that channel inactive; invalid imports or configuration fail startup. `AVA_IM_DISABLED_ADAPTERS` disables channels. WeChat's one-off interactive QR login (`python -m services.entrypoints.im_bridge.adapters.weixin_login --login`) lives in `adapters/weixin_login.py`. Feishu inbound: WS events + polling (`feishu_poll_cursor.py`) and the ws-proxy handshake (`feishu_ws_proxy.py`) — [[feishu-inbound.ava.okf.md]].
- **Dialog push**: SSE only wakes a committed `GET /timeline` read; periodic pulls cover pre-commit events with no later SSE. The dialog filter selects user-originated `inbound_chat` and `agent_chat`. Qualified persistent source/block identities are accepted into the shared IM Outbox in the same transaction as the per-chat watermark. Timestamp ordering survives compact renumbering; numeric item positions break ties. Unqualified legacy sources hold acceptance instead of resetting the cursor. Explicit switch invocations retain immutable replay batch receipts, including empty selections. The worker sends frozen adapter manifests and retains ambiguous attempts without automatic replay; see [[services/docs/gateway_side/im-outbound-retry/im-outbound-retry.ava.okf.md]].
- **Normal notice polling** (`notice_bridge.py`, `notice_poll_store.py`): Telegram owner notifications freeze their recipient, account, rendering and buttons in the same transaction as source receipts and diagnostic progress. A fixed cutover floor (max ID at first init) plus receipt anti-join handles late lower-ID commits; ID maxima do not establish commit order. Older notices stay listable via `/notice`. No-owner acceptance holds; filtered decisions persist. Explicit `/notice` listing remains a separate immediate producer; see [[services/docs/gateway_side/im-outbound-retry/notice-poll.ava.okf.md]].
- **Command set**: `/list` / `/switch` / `/status` / `/spawn` / `/commands` / `/help`, plus `/notice` (notice queue) — see `core.py`; initialized selection is canonical in `im_bridge_cursors`; `state.py` keeps its derived JSON cache.
- **Spawn menu** (`spawn_menu.py`): interactive agent-spawn flow from the chat (`SpawnDraft` lives in `types.py`; `SpawnMenuMixin` owns the flow). Creation event keys and draft recovery limits: [[im-spawn-menu.ava.okf.md]].
- **Push watchdog** (`push_watchdog.py`): context-token failure alerts and recovery hints. Only adapter-proven unstarted sends receive a bounded retry; see [[services/docs/gateway_side/im-outbound-retry/im-outbound-retry.ava.okf.md]].
- **Push health sampling** (`outbound/health.py`): one structured `im_push_health` event per configured channel (`AVA_IM_PUSH_HEALTH_CHANNELS`, default the Telegram/Feishu legs) each `AVA_IM_PUSH_HEALTH_INTERVAL_SECONDS` (default 60) — seconds since the channel's newest delivered push (`last_ok_age_s`; the negative `PUSH_HEALTH_NEVER_SENT` sentinel when none is on record) and pushes still waiting for a delivery attempt (`pending`, the durable outbox's `queued`+`sending` rows). Detection only: the leg-staleness alert rule, its threshold and its notification live in Grafana (task #4935).
- **Configuration** (`config.py`, `daemon.py`): the bridge reads no `settings` outside `daemon.py`, its composition root. The root builds three frozen slices from the flat fields — `ImBridgeConfig` (core, gateway client, push watchdog, notice bridge), `TelegramCredentialsConfig`, `FeishuCredentialsConfig` — and the gateway client from the gateway URL and the machine API token's Bearer (`gateway_auth_headers()`; the bridge never logs in and never holds the cluster secret), and passes them through constructors. `/send` accepts the write generation's machine API tokens (`daemon_acceptance`). `meizel/state/ambient_state` (`settings-read`) fails any other module of the package that imports `settings`. See `future/infra/security/dependency-injection.md`.
- **Ops-alert fan-out**: `IMBridgeCore.notify_user` sends ops alerts (P0/P1 by default) to the owner chat — see [[gateway/alerts/docs/alerts.ava.okf.md]]. Each channel retries only a proven unstarted send; uncertain or partial failures stop; a failed channel still reports `error: <TypeName>` (all-failed → `/send` 502 → the caller's `notified_at` re-send gate, unchanged). Feishu has no configured owner id — its leg resolves the last p2p sender, an open id held only in memory — so `FeishuAdapter.start(tasks)` seeds it from the persisted switch state's single `feishu:<open_id>` key when exactly one is recorded, never overwriting an owner already known; with none or several recorded the leg stays blind and the user is alerted over Telegram (`copy.py`; task #4930).

- [[feishu-lifecycle.ava.okf.md]]

## Key Dependencies
- [[gateway/routers/docs/routers.ava.okf.md]] — the gateway REST client + SSE subscription the bridge consumes
- [[gateway/alerts/docs/alerts.ava.okf.md]] — IM notification fan-out for ops alerts

## Entry Points
- `services/entrypoints/im_bridge/daemon.py` — `.venv/bin/python -m services.entrypoints.im_bridge.daemon`
- `services/entrypoints/im_bridge/core.py` — shared envelope / command routing / SSE push (`IMBridgeCore`)
- `services/entrypoints/im_bridge/gateway_client.py` — gateway REST and SSE transport (`GatewayClient`)
- `services/entrypoints/im_bridge/state.py` — persisted per-chat switch state
- `services/entrypoints/im_bridge/cursor_store.py` — compatibility push positions and the independent Feishu inbound poll cursor
- `services/entrypoints/im_bridge/outbound/{store,health,worker,types}.py` — atomic timeline acceptance and shared delivery lifecycle (also the periodic push-health sample)
- `services/entrypoints/im_bridge/notice_poll_store.py` — normal notice cutover and source receipts
- `services/entrypoints/im_bridge/types.py` — shared message, chat-state, and adapter contracts
- `adapters/feishu_inbound.py` — SDK event payload normalization
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`supervision/roster/healthz.py`)

## Notes
- Bridge ↔ agent dialog is human messages in, agent text out — tool execution / reasoning / other agents' messages are never pushed to IM

## Weixin inbound identity

Weixin sources commit native receipts and frozen routes before cursor checkpoint.
A new account binding drains unproven backlog into quarantine, then activates on
a committed real empty response. Unproven commands remain uncertain.
See [[weixin-inbound-identity]].


## Task ownership and failures

See [[im-bridge-lifecycle]] for service-owned tasks, explicit retry boundaries
and strict local state restoration.
