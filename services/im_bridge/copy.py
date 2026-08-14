"""IM Bridge user-facing copy — every string a user sees in chat.

Converged here so the IM surface has one voice: English, with command names
kept as-is. A string that is not in this module (or a dynamic composition
built from it) is a governance violation — keep it that way.

Naming: <SURFACE>_<WHAT>. Values are plain strings; f-string composition
happens at the call site with copy constants as the template.
"""

# -- generic ---------------------------------------------------------------

QUEUED_NOTICE = "⚠️ Gateway temporarily unavailable — messages queued, will retry automatically"  # emoji-ok: warning glyph the user sees in chat

# -- /list /switch /status /commands /help ---------------------------------

NO_LIVE_AGENTS = "No live agents."
LIVE_AGENTS_TITLE = "Live agents — tap one to switch:"
LIVE_AGENTS_TITLE_BUTTONS = "Live agents — tap one to switch"
SWITCH_USAGE = "Usage: /switch <agent id>. Run /list to pick an agent to switch to."
AGENT_NOT_FOUND = "Agent not found: {arg}. Run /list to see live agents."
AGENT_CANNOT_SWITCH = "Agent {agent_id} is {status} — cannot switch."
SWITCHED_TO = "Switched to agent {agent_id} ({label})."
SWITCHED_TO_UNNAMED = "Switched to agent {agent_id}."
NO_MESSAGES_YET = "No messages yet."
NO_AGENT_SWITCHED = "Not switched to any agent. Run /list to pick one."
CURRENT_AGENT_GONE = "The current agent no longer exists — cleared."
NO_COMMANDS_REGISTERED = "No commands registered."
COMMANDS_HEADER = "/list /spawn /status /help: IM commands"
COMMANDS_INTRO = "{count} Ava slash commands. Tap one to run, or type /name + instruction:"

HELP_TEXT = (
    "/list: live agents (tap one to switch)\n"
    "/spawn: spawn an agent (layered menu)\n"
    "/status: current agent status details\n"
    "/commands: all Ava slash commands (skills, presets)\n"
    "/help: this list\n"
    "Other text: sent to the current agent"
)

# -- status details ---------------------------------------------------------

UNNAMED_LABEL = "(unnamed)"
STATUS_DETAIL_LINE = "agent {agent_id}: {label}"
STATUS_STATE_LINE = "status: {status}"

STATUS_LABELS = {
    "machine": "machine",
    "spawned_at": "spawned",
    "started_at": "started",
    "last_active_at": "last active",
    "pid": "pid",
}

# -- /spawn menu ------------------------------------------------------------

SPAWN_LAYER_PRESET = "Spawn (1/3): preset:"
SPAWN_LAYER_MODEL = "Spawn (2/3): model:"
SPAWN_LAYER_EFFORT = "Spawn (3/3): reasoning effort:"
SPAWN_BUTTON_NO_PRESET = "No preset"
SPAWN_BUTTON_PROVIDER_DEFAULT = "Default"
SPAWN_BUTTON_SUMMARY_PREFIX = "Spawn:"
SPAWN_BUTTON_DEFAULT_VALUE = "Default"
SPAWN_NO_MODELS = "No models available: spawn failed before it started."
SPAWN_PRESET_GONE = "That preset no longer exists: start over with /spawn."
SPAWN_UNKNOWN_ACTION = "Unknown menu action: start over with /spawn."
SPAWN_NOTHING_TO_SPAWN = "Nothing to spawn: run /spawn to start."
SPAWN_FAILED = "Spawn failed: {exc}"
SPAWNED_WITH_PRESET = "Spawned {preset} (agent #{agent_id})."
SPAWNED_PLAIN = "Spawned agent #{agent_id}."
SPAWNED_TAP_TO_SWITCH = "Tap the button to switch:"
SPAWN_SWITCH_BUTTON = "Switch to #{agent_id}"

# -- notice reply -----------------------------------------------------------

REPLY_RESOLVE_FAILED = "⚠️ Reply failed to send (the notice may already be handled). Exited reply mode; the message was not lost."  # emoji-ok: failure hint (user-facing)
REPLY_SENT = "✅ Sent as a reply to that notice"  # emoji-ok: reply confirmation (user-facing)
REPLY_SENT_OTHER_AGENT = "⚠️ That notice belongs to agent #{agent_id} (this conversation is #{switched}) — its reply will not reach you here; /switch {agent_id} to talk to it"  # emoji-ok: cross-agent hint (user-facing)

# -- push watchdog ----------------------------------------------------------

PUSH_FAILURE_ALERT = '「{channel}」 push link failed {failures} times consecutively. Send a message to the {channel} bot (e.g. "hi") to restore the push; if messages still do not arrive, the QR login must be redone — contact the administrator.'
PUSH_RECOVERED_HINT = (
    "(system note: the '{channel}' push link failed earlier and has now recovered)"
)

# -- alert push (shared/alerts.py notifies through the IM bridge) -------------

# Alert push templates, per language. Language follows the UI language —
# user_settings ``display.language`` ("zh" | "en"; missing/unknown falls back
# to ALERT_LANGUAGE_DEFAULT, user ruling 2026-08-13). Only template/framework
# copy is translated: alert labels/annotations data (severity, alertname,
# summary, generator_url) passes through untranslated. The English set keeps
# the pre-ruling production strings; {severity}/{alertname}/{time}/{url} are
# filled in at the call site.
ALERT_LANGUAGES = ("zh", "en")
ALERT_LANGUAGE_DEFAULT = "zh"

ALERT_HEAD = {
    "zh": {
        "firing": "⚠️ 告警 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ 已恢复 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
    "en": {
        "firing": "⚠️ ALERT [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ RESOLVED [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
}
ALERT_TRIGGERED_AT = {
    "zh": "触发时间 {time}",
    "en": "triggered {time}",
}
ALERT_JUMP_LINK = "→ {url}/insights/alerts"
