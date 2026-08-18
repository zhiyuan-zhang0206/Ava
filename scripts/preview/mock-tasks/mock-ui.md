# Mock UI Task

You are a preview agent testing the UI surface. Spin up a web page to show the
user that the preview cluster is operational.

## Steps

1. **Create a simple UI page**:
   ```python
   ava.ui.page(
       title="Preview Cluster — Operational",
       content="""
# Preview Cluster Status

The Ava preview cluster on WSL is **operational**.

- **Cluster**: preview
- **Machine**: a Linux/WSL runner host
- **Branch**: develop

## Sample Agents Active
- Coding agent (PR workflow mock)
- Chat agents ×3 (graph activity)
- Notice agent (notice lifecycle)

> This page was rendered by a sample agent to verify the UI surface works.
""",
   )
   ```

2. **Log**: `ava.self.log("Mock UI page rendered — preview cluster UI surface OK")`

3. **Wait 10 seconds**, then close the page and **terminate**.
