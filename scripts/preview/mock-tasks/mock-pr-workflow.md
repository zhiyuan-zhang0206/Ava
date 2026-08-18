# Mock PR Workflow Task

You are a preview agent running on the Ava preview cluster. Your task is to
simulate a simple PR workflow to exercise the agent process, file tools, and
MCP daemon.

## Steps

1. **Create a test file** in your workspace:
   - Write a file at `preview-test-<your-agent-id>.txt` with the content:
     "Preview cluster smoke test — agent <your-id> at <current-iso-timestamp>"
   - Use `ava.files.write()`

2. **Verify the file**:
   - Read it back with `ava.files.read()` and confirm the content matches

3. **Log your progress**:
   - Use `ava.self.log("Preview smoke test: step N/3 complete")`

4. **Report completion**:
   - Write a final status to `preview-test-result-<your-agent-id>.txt`:
     "PASS: preview-cluster PR workflow mock completed successfully"

## Remember
- You are on the PREVIEW cluster — your actions do NOT affect production
- This is a mock exercise — no actual PR needs to be created
- Keep your response brief; just complete the steps and report done
- Use `ava.self.log()` at each milestone

When you finish all steps, terminate yourself with `ava.self.terminate()`.
