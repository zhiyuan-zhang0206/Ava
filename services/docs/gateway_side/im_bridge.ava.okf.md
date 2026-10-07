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
- **Dialog push**: SSE only wakes a committed `GET /timeline` read; periodic pulls cover pre-commit events with no later SSE. The dialog filter selects user-originated `inbound_chat` and `agent_chat`. Qualified persistent source/block identities are accepted into the shared IM Outbox in the same transaction as the per-chat watermark. Timestamp ordering survives compact renumbering; numeric item positions break ties. Unqualified legacy sources hold acceptance instead of resetting the cursor. Explicit switch invocations retain immutable replay batch receipts, including empty selections. The worker sends frozen adapter manifests and retains ambiguous attempts without automatic replay; see [[im-outbound-retry/im-outbound-retry.ava.okf.md]].
- **Normal notice polling** (`notice_bridge.py`, `notice_poll_store.py`): Telegram owner notifications freeze their recipient, account, rendering and buttons in the same transaction as source receipts and diagnostic progress. A fixed legacy cutover plus receipt anti-join handles late lower-ID commits; cursor maxima do not establish commit order. Missing/corrupt legacy history is retained for manual inspection. No-owner acceptance holds; filtered decisions persist. Explicit `/notice` listing remains a separate immediate producer; see [[im-outbound-retry/notice-poll.ava.okf.md]].
- **Command set**: `/list` / `/switch` / `/status` / `/spawn` / `/commands` / `/help`, plus `/notice` (notice queue) — see `core.py`; initialized selection is canonical in `im_bridge_cursors`; `state.py` keeps its derived JSON cache.
- **Spawn menu** (`spawn_menu.py`): interactive agent-spawn flow from the chat (`SpawnDraft` lives in `types.py`; `SpawnMenuMixin` owns the flow). Creation event keys and draft recovery limits: [[im-spawn-menu.ava.okf.md]].
- **Push watchdog** (`push_watchdog.py`): context-token failure alerts and recovery hints. Only adapter-proven unstarted sends receive a bounded retry; see [[im-outbound-retry/im-outbound-retry.ava.okf.md]].
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
- `services/entrypoints/im_bridge/cursor_store.py` — compatibility push positions and the independent Feishu inbound poll cursor
- `services/entrypoints/im_bridge/outbound_store.py`, `outbound_worker.py` — atomic timeline acceptance and shared delivery lifecycle
- `services/entrypoints/im_bridge/notice_poll_store.py` — normal notice cutover and source receipts
- `services/entrypoints/im_bridge/types.py` — shared message, chat-state, and adapter contracts
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`ops/roster/healthz.py`)

## Notes
- Bridge ↔ agent dialog is human messages in, agent text out — tool execution / reasoning / other agents' messages are never pushed to IM

## Weixin inbound identity

Weixin sources commit native receipts and frozen routes before cursor checkpoint.
Account/history cutover holds, drains unproven backlog, then activates on a
committed real empty response. Unproven commands remain uncertain.
See [[weixin-inbound-identity]].
