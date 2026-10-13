"""The reply text of the scripted message-flow agent.

The e2e fake model (`tests/e2e/fakes/scenarios/message_flow.py`) answers with this
text and the container check (`scripts/verify/observe.py`) waits for it on the timeline,
so the constant lives below both.
"""

REPLY_TEXT = "\u7ed3\u679c\u662f 3\u3002"
