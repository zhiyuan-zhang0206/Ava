# Mock Chat Exchange Task — Agent A

You are Agent A in a 3-agent mock chat on the preview cluster. Your job is to
send messages to Agents B and C to populate FleetView graph activity.

## Setup
- You are `preview-chat-a`
- Agent B is `preview-chat-b` (spawned separately)
- Agent C is `preview-chat-c` (spawned separately)

## Steps

1. **Find your peers**: use `ava.agents.list_agents()` to confirm B and C exist.
   Wait a moment if they haven't appeared yet.

2. **Send a greeting to B**: use `ava.agents.send_message(<b_id>, "Hello from preview-chat-a! This is a mock exchange to exercise the FleetView graph. How are you?")`

3. **Send a greeting to C**: use `ava.agents.send_message(<c_id>, "Hello from preview-chat-a! FleetView graph test — message 1 of 3.")

4. **Wait 10 seconds**, then send a follow-up to B:
   `"Message 2/3: confirming multi-turn exchanges render correctly in FleetView."`

5. **Wait 10 more seconds**, send to C:
   `"Message 3/3: final test message. The FleetView graph should show 3 agents connected by recent messages."`

6. **Log completion**: `ava.self.log("Mock chat exchange complete — 3 messages sent")`

7. **Terminate**: `ava.self.terminate()`

Keep it brief — no need to wait for replies unless you want to confirm receipt.
