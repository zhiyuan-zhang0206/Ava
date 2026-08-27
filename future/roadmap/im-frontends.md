# IM frontends (im_bridge)

An IM app is Ava's **frontend**, not a message channel — the opposite of
OpenClaw's channel abstraction: Ava is a fleet of 21+ agents whose surface
far exceeds conversation (FleetView / task tree / stats), and IM can only
carry "remote control / query / notify" operations. Following Hermes Agent's
layering: the Messaging Gateway belongs in the User Interface Layer (the UI
layer only handles transport / presentation / session routing, no agent
logic).

## Architecture (decided, aligned with the user 2026-08-02)

> **Status note (2026-08-08)**: the v1 command set and channel table below lag the shipped implementation — see [services/gateway_side/im_bridge.ava.okf.md](../../services/gateway_side/im_bridge.ava.okf.md) for the current reality. WeChat (iLink) and Feishu adapters are **production-disabled since 2026-08-06** (`AVA_IM_DISABLED_ADAPTERS=weixin,feishu`); Telegram is the only live channel.

- **IM Bridge = a gateway-hosted service** (`services/im_bridge/`, ServiceSpec
  `im-bridge`): one adapter (service) per IM, sharing a core (message envelope,
  command routing, per-channel in-memory session state, gateway REST + SSE
  clients).
- Every channel is **active outbound** (Telegram long polling / WeChat iLink
  long polling / Feishu WS long connection) with zero exposed ports — usable
  even when the user is off the private network / VPN overlay.
- Command set (v1 -> shipped): `/list` `/switch <id|label>` (auto-pulls the
  latest 5 timeline entries and filters what it pushes) `/status` `/help`
  (shipped builds also have `/spawn` `/commands` `/notice`, plus a spawn menu
  and notification buttons/reply mode); plain text forwards to the current
  agent; no switch means an error, no fallback.
- After a switch, subscribe to all of that agent's new messages (SSE
  `timeline_snapshot` -> filter conversation kinds -> push), follow mode.
- WeChat goes through the **iLink official Bot API** (Tencent opened it via
  OpenClaw in 2026-03; a bot identity is enough for the IM Bridge scenario);
  the OCR approach stays out of the open-source repo.

## v1 channels (landing 2026-08)

| Channel | Access | Credential | Status |
|---|---|---|---|
| Telegram | Bot API (getUpdates long polling) | reuses the existing bot (settings.telegram) | **live (only production channel)** |
| WeChat | iLink (ilinkai.weixin.qq.com, HTTP/JSON + QR login) | QR binding (one-time) | implemented, **production-disabled (2026-08-06)** |
| Feishu | self-built app + WS long connection (lark-oapi) | app_id/app_secret (Ava Corp. enterprise) | implemented, **production-disabled (2026-08-06)** |

## Future extensions (GitHub issue #971)

Same im_bridge core; a new channel = a new adapter:

- **WhatsApp** (Baileys / Cloud API), **Discord** (bot API; the MCP channel was
  removed 2026-08-12, an adapter must be built separately), **Slack**,
  **iMessage** (macOS bridge), **Signal**, **LINE**, **QQ**, **X/Telegram
  groups**, **WeCom** ...
- Interaction primitive upgrades (borrowed from Hermes): confirmation buttons
  (dangerous actions), clarification choices, a command palette — buttons when
  the platform supports them, plain-text degradation otherwise.
- Group-chat support (@ trigger + ambient low-priority context).
- Media send/receive (images/voice), degrading gracefully by platform.

## Boundaries (deliberate-no, not in v1)

- Uploading files, the tasks command, group chat, media forwarding — all
  excluded from v1 (simplicity first).
- No fallback (no switch = error).
- The OCR-based WeChat approach is not open-sourced (Tencent policy risk;
  stays in the private repo).
