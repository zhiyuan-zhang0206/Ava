"""E2EEnv -- frozen data pack passed to test after fixture orchestration completes."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page


@dataclass(frozen=True)
class E2EEnv:
    gateway_url: str
    frontend_url: str
    """frontend base URL, no query. e2e tests normally use agent_url to deep-link to
    the spawned agent; only use bare frontend_url when testing "sidebar auto-select
    fallback without a specified agent"."""

    agent_url: str
    """frontend_url + `?agent_id={agent_id}` deep-link; useAgents reads this query
    param in its mount effect to set init activeId, no longer relying on sidebar
    auto-select (race-prone: must wait for agent fetch before select; early page.goto
    may see an empty sidebar)."""

    page: Page
    agent_id: int
