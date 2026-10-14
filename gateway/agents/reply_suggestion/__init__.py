"""Reply suggestions: a drafted reply to the agent's last message, for the empty composer.

`POST /api/agents/{agent_id}/reply-suggestion` judges whether suggesting a reply would
help the user and, when it would, lets a chat model choose the number and content of candidates.
The web console shows them as grey placeholder text; Up/Down chooses, Right accepts,
and Enter sends the accepted draft.
"""
