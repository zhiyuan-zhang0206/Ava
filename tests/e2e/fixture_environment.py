from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

__all__ = ["E2EEnv"]


@dataclass(frozen=True)
class E2EEnv:
    gateway_url: str
    frontend_url: str
    agent_url: str
    page: Page
    agent_id: int
