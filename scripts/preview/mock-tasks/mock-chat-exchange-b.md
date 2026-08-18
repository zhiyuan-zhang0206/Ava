# Mock Chat Exchange Task — Agent B

You are Agent B (`preview-chat-b`) in a 3-agent mock chat on the preview cluster.
Agent A will send you messages — just acknowledge them to show bidirectional
communication in the FleetView graph.

## Steps

1. **Wait for a message** from preview-chat-a — it should arrive within 30 seconds.

2. **Reply to each message** you receive:
   - "Got it, preview-chat-a! FleetView graph exercise confirmed — reply from B."
   - Keep replies brief.

3. **Log your activity**: `ava.self.log("preview-chat-b: replied to chat messages")`

4. **After 3 replies (or 2 minutes)**, terminate: `ava.self.terminate()`
