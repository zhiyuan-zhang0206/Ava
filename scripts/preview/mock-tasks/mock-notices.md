# Mock Notice Queue Task

You are a preview agent testing the notice queue. Your job is to post notices
at all 3 levels, update one, and dismiss one — exercising the full notice
lifecycle visible in FleetView.

## Steps

1. **Post an FYI notice**:
   ```python
   ava.ui.notify(
       level="info",
       title="Preview Cluster: FYI Notice",
       content="This is a mock FYI notice from the preview cluster. The notice queue is working correctly. No action needed.",
   )
   ```

2. **Post a question notice**:
   ```python
   ava.ui.notify(
       level="question",
       title="Preview Cluster: Question",
       content="Mock question: should we add more sample agents to the preview cluster? This tests the question-level notice rendering.",
   )
   ```

3. **Post a blocking notice**:
   ```python
   ava.ui.notify(
       level="blocker",
       title="Preview Cluster: Blocker (MOCK)",
       content="THIS IS A MOCK BLOCKER. It tests the blocker-level notice rendering in FleetView. Nothing is actually broken. This notice will be dismissed in the next step.",
   )
   ```

4. **Wait 5 seconds**, then **dismiss the blocker** (use `ava.ui.notify` dismiss).

5. **Wait 5 more seconds**, **update the FYI** to say "FYI updated — notices working."

6. **Log completion**: `ava.self.log("Mock notice lifecycle complete — 3 posted, 1 dismissed, 1 updated")`

7. **Terminate**: `ava.self.terminate()`

Keep it brief — don't wait for user responses to the question notice.
