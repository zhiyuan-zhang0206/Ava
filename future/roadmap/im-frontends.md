# IM frontends (im_bridge)

IM 应用是 Ava 的**前端**（frontend），不是消息通道（channel）——与 OpenClaw 的
channel 抽象相反：Ava 是 21+ agents 的舰队，功能面远超对话（FleetView / 任务树 /
统计），IM 只能承载"遥控 / 查询 / 通知"类操作。参考 Hermes Agent 的分层：
Messaging Gateway 属于 User Interface Layer（UI 层只管 transport / presentation /
session routing，不含 agent 逻辑）。

## 架构（已定，2026-08-02 与用户对齐）

> **Status note (2026-08-08)**: the v1 command set and channel table below lag the shipped implementation — see [services/gateway_side/im_bridge.ava.okf.md](../../services/gateway_side/im_bridge.ava.okf.md) for the current reality. WeChat (iLink) and Feishu adapters are **production-disabled since 2026-08-06** (`AVA_IM_DISABLED_ADAPTERS=weixin,feishu`); Telegram is the only live channel.

- **IM Bridge = gateway 托管的 service**（`services/im_bridge/`，ServiceSpec
  `im-bridge`）：每个 IM 一个 adapter（service），共享核心（消息信封、命令路由、
  per-channel 内存会话状态、gateway REST + SSE 客户端）。
- 所有通道**主动出站**（Telegram 长轮询 / 微信 iLink 长轮询 / 飞书 WS 长连接），
  零端口暴露——用户不在 tailnet 时也能用。
- 命令集（v1 → shipped）：`/list` `/switch <id|label>`（自动拉最近 5 条 timeline 过滤推送）
  `/status` `/help`（已 shipped 版本另有 `/spawn` `/commands` `/notice`，加 spawn
  菜单与通知按钮/回复模式）；普通文本转发给当前 agent；未 switch 报错，无 fallback。
- switch 后订阅该 agent 的全部新消息（SSE `timeline_snapshot` → 过滤对话类 →
  推送），跟随模式。
- 微信走 **iLink 官方 Bot API**（腾讯 2026-03 通过 OpenClaw 开放，bot 身份对
  IM Bridge 场景足够）；OCR 方案不进开源 repo。

## v1 通道（2026-08 落地中）

| 通道 | 接入 | 凭证 | 状态 |
|---|---|---|---|
| Telegram | Bot API（getUpdates 长轮询） | 复用现有 bot（settings.telegram） | **live（唯一生产通道）** |
| 微信 | iLink（ilinkai.weixin.qq.com，HTTP/JSON + QR 登录） | 扫码绑定（一次性） | 已实现，**生产禁用（2026-08-06）** |
| 飞书 | 自建应用 + WS 长连接（lark-oapi） | app_id/app_secret（Ava Corp. 企业） | 已实现，**生产禁用（2026-08-06）** |

## 未来扩展（对应 GitHub issue #971）

同一 im_bridge 核心，新增通道 = 新 adapter：

- **WhatsApp**（Baileys / Cloud API）、**Discord**（bot API；MCP 通道已于 2026-08-12 删除，需另行实现 adapter）、
  **Slack**、**iMessage**（macOS 桥）、**Signal**、**LINE**、**QQ**、**X/Telegram
  群组**、**企业微信**……
- 交互原语升级（借鉴 Hermes）：确认按钮（危险操作）、澄清选择、命令面板——
  平台支持时用按钮，否则降级纯文本。
- 群聊支持（@ 触发 + ambient 低优先级上下文）。
- 媒体收发（图片/语音），按平台能力优雅降级。

## 边界（deliberate-no，v1 不做）

- 上传文件、tasks 命令、群聊、媒体转发 —— v1 全部排除（大道至简）。
- 不做 fallback（未 switch 就是报错）。
- 不开源 OCR 微信方案（腾讯 policy 风险，留私有仓）。
