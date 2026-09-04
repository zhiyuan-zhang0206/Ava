"""Computer-use MCP service — the desktop automation layer (task #1101).

One per-machine daemon (`services/computer/mcp_daemon.py`), backed by focused
screen, OCR, execution, and error modules, executes every desktop action
through the signed permissions helper — the single TCC
grant-holder — serializing actions machine-wide, coordinating screen
ownership (lease + FIFO queue + release_control), and auditing every action
as a `computer_action` event. There is no code-enforced governance: per-agent
permission division is a prompt-level peer convention (user ruling
2026-08-10); the cluster's security boundary is its entry point. Per-agent
bridges (`mcp_wrapper.py`, and the MCP daemon's direct dial in
`ava/_mcp_computer.py`) reach it over the `computer-mcp` Unix socket.
"""
