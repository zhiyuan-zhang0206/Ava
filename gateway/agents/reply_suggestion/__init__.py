"""Reply suggestions: a drafted reply to the agent's last message, for the empty composer.

`POST /api/agents/{agent_id}/reply-suggestion` runs a decision-model gate ("is the agent
waiting for the user?") and, when it passes, a small chat model that drafts reply candidates.
The web console shows them as grey placeholder text; Up/Down chooses, Right accepts,
and Enter sends the accepted draft.
"""
