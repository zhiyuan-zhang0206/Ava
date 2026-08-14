# Mock Chat Exchange Task — Agent C

You are Agent C (`preview-chat-c`) in a 3-agent mock chat on the preview cluster.
Agent A will send you messages — reply briefly to show activity.

## Steps

1. **Wait for messages** from preview-chat-a.

2. **Reply to each** with a short acknowledgment.

3. **Log**: `ava.self.log("preview-chat-c: chat activity complete")`

4. **Terminate** after replying: `ava.self.terminate()`
