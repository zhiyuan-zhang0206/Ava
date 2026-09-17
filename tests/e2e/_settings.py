"""User-settings helpers for e2e tests.

The frontend resolves an unset setting from `USER_SETTING_DEFAULTS`
(`ui/web/src/lib/types.ts`), so a fresh e2e cluster renders with the product
defaults — and a default is a user-ruling-changed value, not a test fixture.
A test that asserts on rendering which depends on a specific setting pins it
explicitly through the same REST surface the frontend uses
(`PUT /api/settings/{key}`), before the first `page.goto` (the app fetches
settings once on load).
"""

from __future__ import annotations

import httpx


def pin_expand_runs_all(gateway_url: str) -> None:
    """Pin the Details level to "all" — detail blocks expanded.

    The unset default is "none" (collapsed, user ruling 2026-09-17), which
    folds secondary timeline items (compact envelopes, thinking, code) out of
    the DOM. Tests that assert on that content opt into expansion first.
    """
    resp = httpx.put(
        f"{gateway_url}/api/settings/display.expand_runs_mode",
        json={"value": "all"},
        timeout=30.0,
    )
    resp.raise_for_status()
