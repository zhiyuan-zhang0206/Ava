# Discord MCP fix — safe defaults for privileged intents

> **Note (2026-08-12):** The Discord MCP was deleted entirely per the user
> ruling — Ava no longer maintains it. This document is retained as history
> only.

## Context

After Ava update (2026-07-15), Discord MCP was broken: every tool call returned
"Discord client not ready". The bot process was running with TCP connected to
Discord gateway, but `on_ready` never fired.

## Root causes

### 1. discord.py v2.7.1 — PrivilegedIntentsRequired is now a hard error

In earlier discord.py versions, requesting intents not enabled in the Developer
Portal produced a warning; the bot connected but the privileged features silently
didn't work. In v2.7.1, the gateway close code 4014 ("Disallowed intent(s)") is
raised as `PrivilegedIntentsRequired` — the bot refuses to connect at all.

This bot's Developer Portal does NOT have `message_content` or `members`
privileged intents enabled.

### 2. Unsafe defaults in mcps/discord/server.py

The env-var-controlled intent toggles defaulted to empty string:
```python
_DISABLE_MSG_CONTENT = os.getenv("DISCORD_DISABLE_MESSAGE_CONTENT", "").strip() in ("1", "true", "yes")
```

Empty string does not match any of `("1", "true", "yes")` → `False` → intents
ENABLED by default → gateway 4014.

### 3. Machine config override of built-in config (secondary)

`~/.ava/mcp.json` had `uv run mcp-discord` (upstream package, no `on_ready` fix)
instead of `uv run python -m mcps.discord` (custom fixed module). The built-in
`.mcp.json` had the correct command, but the machine config overrode it.

## Fix

1. **server.py**: Change default from `""` to `"1"` for both
   `DISCORD_DISABLE_MESSAGE_CONTENT` and `DISCORD_DISABLE_MEMBERS`
2. **.mcp.json**: Remove unresolvable `${DISCORD_TOKEN}` placeholder; set
   safe defaults (`"1"`)
3. **Ops fix**: Update `~/.ava/mcp.json` to use correct command and disable
   privileged intents

## Update resilience

The code defaults are now safe (privileged intents disabled). Future discord.py
upgrades won't break the bot, because:
- If intents are disabled, the bot connects regardless of Developer Portal settings
- If user enables intents in Developer Portal, they must explicitly set env vars to `"0"`

PR: #476 (internal tracker; see CONTRIBUTING.md)
