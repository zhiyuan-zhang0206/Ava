---
type: doc
title: IM Bridge — the product frontend
description: "The IM Bridge (`services/entrypoints/im_bridge/`) is the product frontend: every user-facing IM channel (Telegram / WeChat / Feishu) is an adapter the bridge owns — message envelope, command routing, per-chat switch state, SSE subscription push of agent dialog."
tags: []
---

# IM Bridge — the product frontend

## What it is
The IM Bridge daemon runs every configured IM channel adapter (Telegram / WeChat / Feishu — one adapter per channel, sharing the bridge core). Since 2026-08 the bridge **is the product frontend** (user ruling: IM is the only frontend; the Telegram skill was removed — see `docs/decisions/runtime/processes/shutdown/2026-08-03-telegram-skill-removed.md`): user-facing channels are not a side feature but the primary surface.

**Role affiliation**: gateway side — the roster entry in `ops/roster/__init__.py` declares `ServiceSpec.capabilities=_GATEWAY` and `requires_db=True` (R3 door ④: notice_bridge reads and lazily expires `agent_notices` directly — previously declared False with the drift noted in-okf; the spec now matches). Kept alive by the root supervisor's health monitor through the roster's `/healthz` identity probe.

## Core Responsibilities
- **Adapters** (`services/entrypoints/im_bridge/adapters/<channel>.py`): each channel is one adapter sharing the core — envelope, command routing, per-channel session state. An adapter whose module is missing or credentials unset logs "skipped"; the daemon keeps serving the others. `AVA_IM_DISABLED_ADAPTERS` disables channels. WeChat's one-off interactive QR login (`python -m services.entrypoints.im_bridge.adapters.weixin --login`) lives in `adapters/weixin_login.py`. Feishu inbound is dual-path: `im.message.receive_v1` WS events plus a ListMessage polling fallback (`AVA_FEISHU_POLL_INTERVAL_SECONDS` / `AVA_FEISHU_POLL_CHAT_ID`) for platforms that never push events — a shared seen-set and the gateway's keyed delivery (`feishu:<message_id>`) keep the two paths idempotent. The poll cursor (message id + create time) is persisted in `im_bridge_cursors` (`adapters/feishu_poll_cursor.py`, `cursor_store.py`): a restarted bridge pages back through the messages that arrived while it was down, until the cursor or the agent side's stale-chat-inbound window (`delivery_watchdog_stale_claimed_threshold_seconds`) is reached; only a chat never polled before seeds without replaying history. The WS handshake honors the machine's proxy environment (`HTTP(S)_PROXY` / `ALL_PROXY`, `NO_PROXY` honored per target): the SDK pins `proxy=None` on websockets>=15 and `lark.ws.Client` exposes no proxy parameter, so `adapters/feishu_ws_proxy.py` replaces the SDK's module-level connect-kwargs builder (issue #2089).
- **Dialog push**: cold loads read `GET /timeline`, live updates come from `timeline_snapshot` SSE events; both render through the same dialog filter (`inbound_chat` with `source='user'` + `agent_chat`), so a switch's recent-message replay (`services.im_bridge_replay_messages`, default 5) and the pushed tail agree item-for-item. The per-chat push watermark is persisted in `im_bridge_cursors` as a pair (`push_created_at`, `push_item_id`): `created_at` is the primary key — monotone, and immune to the compact that renumbers a session's `item_id` positions — with `item_id` as the tie-break between one message's blocks; a legacy row without a stamp compares by `item_id` and recovers through the rollback reset. The SSE feed is a live tail, so on daemon start and on every SSE reconnect the timeline past the saved watermark is pushed (`_catch_up`; bounded by `im_bridge_timeline_window`), and a chat with no saved watermark pushes nothing until its next snapshot. A non-empty batch entirely behind the watermark is quiet when both positions carry stamps (the batch is old content — a compact wiping the session's ids is not an incident); only a legacy row without a stamp, or a batch max without one, still runs the numbering check, and a strict rollback there logs ERROR with the full position and resets the watermark to the observed max — the triggering batch is skipped, never replayed (`_reset_watermark_on_rollback`, task #4933: id-only watermarks froze two chats silently for hours on 2026-10-03). `_PUSH_LIMIT` splits long messages.
- **Command set**: `/list` / `/switch` / `/status` / `/spawn` / `/commands` / `/help`, plus `/notice` (notice queue) — see `core.py`; per-chat switch state persists through `state.py` across daemon restarts.
- **Spawn menu** (`spawn_menu.py`): interactive agent-spawn flow from the chat (`SpawnDraft` lives in `types.py`; `SpawnMenuMixin` owns the flow).
- **Push watchdog** (`push_watchdog.py`): context-token failure alerts and recovery hints. Only adapter-proven unstarted sends receive a bounded retry; see [[im-outbound-retry.ava.okf.md]].
- **Configuration** (`config.py`, `daemon.py`): the bridge reads no `settings` outside `daemon.py`, its composition root. The root builds three frozen slices from the flat fields — `ImBridgeConfig` (core, gateway client, push watchdog, notice bridge), `TelegramCredentialsConfig`, `FeishuCredentialsConfig` — and the gateway client from the gateway URL and the machine API token's Bearer (`gateway_auth_headers()`; the bridge never logs in and never holds the cluster secret), and passes them through constructors. `/send` accepts the write generation's machine API tokens (`daemon_acceptance`). `scripts/structure/ambient_state` (`settings-read`) fails any other module of the package that imports `settings`. See `future/infra/security/dependency-injection.md`.
- **Ops-alert fan-out**: `IMBridgeCore.notify_user` sends ops alerts (P0/P1 by default) to the owner chat — see [[gateway/alerts/docs/alerts.ava.okf.md]]. Each channel retries only a proven unstarted send; uncertain or partial failures stop; a failed channel still reports `error: <TypeName>` (all-failed → `/send` 502 → the caller's `notified_at` re-send gate, unchanged). Feishu has no configured owner id — its leg resolves the last p2p sender, an open id held only in memory — so `FeishuAdapter.start()` seeds it from the persisted switch state's single `feishu:<open_id>` key when exactly one is recorded, never overwriting an owner already known; with none or several recorded the leg stays blind and the user is alerted over Telegram (`copy.py`; task #4930).

## Key Dependencies
- [[gateway/routers/docs/routers.ava.okf.md]] — the gateway REST client + SSE subscription the bridge consumes
- [[gateway/alerts/docs/alerts.ava.okf.md]] — IM notification fan-out for ops alerts

## Entry Points
- `services/entrypoints/im_bridge/daemon.py` — `.venv/bin/python -m services.entrypoints.im_bridge.daemon`
- `services/entrypoints/im_bridge/core.py` — shared envelope / command routing / SSE push (`IMBridgeCore`)
- `services/entrypoints/im_bridge/gateway_client.py` — gateway REST and SSE transport (`GatewayClient`)
- `services/entrypoints/im_bridge/state.py` — persisted switch state and inbound outbox
- `services/entrypoints/im_bridge/cursor_store.py` — the push watermark and Feishu poll cursor in Postgres (`im_bridge_cursors`)
- `services/entrypoints/im_bridge/types.py` — shared message, chat-state, and adapter contracts
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`ops/roster/healthz.py`)

## Notes
- Bridge ↔ agent dialog is human messages in, agent text out — tool execution / reasoning / other agents' messages are never pushed to IM
