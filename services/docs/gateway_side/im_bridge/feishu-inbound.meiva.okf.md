---
type: doc
title: "Feishu inbound: WS events and ListMessage polling"
description: "The Feishu dual inbound path: seen-set idempotency, the durable poll cursor and its replay bounds, and the ws-proxy handshake."
tags:
- im-bridge
- feishu
---

# Feishu inbound: dual path

Feishu inbound is dual-path: `im.message.receive_v1` WS events plus a ListMessage polling fallback (`MEIVA_FEISHU_POLL_INTERVAL_SECONDS` / `MEIVA_FEISHU_POLL_CHAT_ID`) for platforms that never push events — a shared seen-set and the gateway's keyed delivery (`feishu:<message_id>`) keep the two paths idempotent. The poll cursor (message id + create time) is persisted in `im_bridge_cursors` (`adapters/feishu_poll_cursor.py`, `cursor_store.py`): a restarted bridge resumes each saved chat from its cursor over one list page (20; an older backlog is lost), dropping messages past the agent's stale-chat-inbound window (`delivery_watchdog_stale_claimed_threshold_seconds`); only a never-polled chat seeds without replay. The WS handshake honors the machine's proxy environment (`HTTP(S)_PROXY` / `ALL_PROXY`, `NO_PROXY` honored per target): the SDK pins `proxy=None` on websockets>=15 and `lark.ws.Client` exposes no proxy parameter, so `adapters/feishu_ws_proxy.py` replaces the SDK's module-level connect-kwargs builder (issue #2089).

Failed poll rounds back off instead of exiting the service; the failure classes live in [[im-bridge-lifecycle.meiva.okf.md]] (task #5052).
