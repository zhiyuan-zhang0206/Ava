"""Reply suggestions: a drafted reply to the agent's last message, for the empty composer.

`POST /api/agents/{agent_id}/reply-suggestion` runs a decision-model gate ("is the agent
waiting for the user?") and, when it passes, a small chat model that drafts the reply. The
web console shows the draft as grey text; nothing is sent until the user accepts and sends it.
"""
