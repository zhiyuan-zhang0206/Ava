# Preview Cluster Validation Suite

You are a validation agent responsible for comprehensively testing the Ava core functionality of the preview cluster.
Execute each group of tests below in order, recording [PASS] or [FAIL] + reason for each.
Finally, summarize the report.

**Important Rules**:
- Each test is independent; a failure in one does not block subsequent tests.
- Record detailed failure reasons (error message, status code, etc.).
- All spawned agents should use `spawner: "preview-validation"`.
- After testing, terminate all child agents for cleanup.
- Do not terminate yourself — wait for me to judge the results.

---

## 1. Agent Lifecycle Tests

### 1.1 spawn
Use `ava.agents.spawn(prompt="reply 'pong' then idle and wait. Do not terminate.")` to create an agent.
After waiting 5 seconds, check if `ava.agents.get_status(id)` is running/idling.
Record: `[PASS] spawn: agent {id}` or `[FAIL] spawn: {reason}`.

### 1.2 send message
Use `ava.agents.send_message(child_id, "ping")` to send a message.
After waiting 5 seconds, check the agent status.
Record: `[PASS] send_message` or `[FAIL]: {reason}`.

### 1.3 terminate
Terminate using `ava.agents.terminate(child_id)`.
After waiting 3 seconds, confirm status == 'terminated'.
Record: `[PASS] terminate` or `[FAIL]: {reason}`.

### 1.4 resurrect
Resurrect using `ava.agents.resurrect(child_id, prompt="reply 'resurrected' then idle. Do not terminate.")`.
After waiting 5 seconds, confirm status in (running, idling).
Record: `[PASS] resurrect` or `[FAIL]: {reason}`.

### 1.5 restart
Restart using `ava.agents.restart(child_id)`.
After waiting 5 seconds, confirm status in (running, idling).
Record: `[PASS] restart` or `[FAIL]: {reason}`.

### 1.6 fork
Fork an agent using `ava.agents.spawn(prompt="reply 'forked' then idle. Do not terminate.", fork_from=child_id)`.
After waiting 5 seconds, confirm the new agent is normal.
Record: `[PASS] fork: agent {fork_id}` or `[FAIL]: {reason}`.

---

## 2. Agent Communication Tests

### 2.1 agent chat
Spawn agent Alice (prompt="You are Alice. Wait for Bob to message you, reply when you receive it, then idle. Do not terminate.")
Spawn agent Bob (prompt=f"Send Alice a message with ava.agents.send_message({alice_id}, 'Hello from Bob'), then idle after her reply.")
After waiting 10 seconds, confirm both agents are alive.
Record: `[PASS] agent-chat` or `[FAIL]: {reason}`.

### 2.2 get_neighbors
Use `ava.agents.get_neighbors(alice_id)` to find neighbors.
Record: `[PASS] get_neighbors: {n} found` or `[FAIL]: {reason}`.

---

## 3. Tool Call Tests

### 3.1 files
Write a test file → read back → verify content → delete.
Record: `[PASS] files` or `[FAIL]: {reason}`.

### 3.2 shell
Execute `ava.shell.run("echo hello-validation")`, verify output.
Record: `[PASS] shell` or `[FAIL]: {reason}`.

### 3.3 web search
Execute `ava.web.search(["Python programming"])`, verify results.
Record: `[PASS] web.search` or `[FAIL]: {reason}`.

---

## 4. Notification System Test

### 4.1 notify → edit → dismiss
Create notification → edit → dismiss.
Record: `[PASS] notices` or `[FAIL]: {reason}`.

---

## 5. Cleanup

Terminate all test agents (child_id, fork_id, alice_id, bob_id).

---

## 6. Summary Report

Write the test results to the absolute path `{{REPORT_PATH}}` (your cwd is your own
workspace, so a relative path would not land where the operator looks):

```markdown
# Preview Validation Report
**Time**: {time}
**Cluster**: preview

| # | Test | Result | Details |
|---|---|---|---|
| 1.1 | spawn | PASS/FAIL | ... |
| ... | ... | ... | ... |

**Total**: X/Y passed
```

Then notify using `ava.ui.notify(title="Preview Validation Completed", content="X/Y passed. Details in {{REPORT_PATH}}")`.

Stay idle, do not terminate.
