"""The page-server session name: one grammar, owned here.

An `ava.ui.serve` page's server runs in a persistent shell session of its
agent, named like any agent shell with a `page-<slug>` label:
`ava-agent-<agent_id>-shell-<session_id>-page-<slug>`. Three sites read that
grammar, all through this module:

- the page-server daemon builds each page's session name (`page_session_name`);
- `ava.shell.sessions` refuses the `page-` label for an ordinary shell, so the
  label names page sessions exactly (`is_page_label`);
- a terminate's shell-session kill (`ops.cluster_status.kill_agent_shells`)
  spares page sessions — a page keeps its own lifecycle (`is_page_label`).
"""

from __future__ import annotations

import re

from base.cluster import session_name

PAGE_LABEL_PREFIX = "page-"

# The PTY session-name contract every agent shell satisfies.
_PTY_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")


def page_session_name(agent_id: int, page_name: str, session_index: int) -> str:
    """The shell session name for one page of `agent_id`, at its allocated index."""
    slug = page_name.lower().replace("_", "-")
    full = f"{session_name(f'agent-{agent_id}')}-shell-{session_index}-{PAGE_LABEL_PREFIX}{slug}"
    if _PTY_NAME_RE.fullmatch(full) is None:
        raise ValueError(f"page session name {full!r} does not match the PTY name contract")
    return full


def is_page_label(label: str | None) -> bool:
    """Whether an agent shell's `-<label>` suffix names a page-server session."""
    return label is not None and label.startswith(PAGE_LABEL_PREFIX)
