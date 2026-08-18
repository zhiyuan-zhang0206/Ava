"""Converge step: block NEXT_PUBLIC_* build-time overrides that poison the bundle.

Split out of ``_converge.py`` (step-module pattern, like ``_converge_firewall``)
to keep the aggregator under the file-size ceiling.
"""

# `next build` / `next start` load these env files; an untracked override here
# bakes NEXT_PUBLIC_* into the JS bundle and silently beats the runtime gateway
# inference in ui/web/src/lib/api.ts — a leftover Vercel-era .env.production
# pointed every browser at a retired host's API (a past prod outage). Only the
# tracked .env.development is legitimate; it affects `next dev` only.
from __future__ import annotations

import re
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx

_FORBIDDEN_FRONTEND_ENV_FILES = (".env", ".env.local", ".env.production", ".env.production.local")

_NEXT_PUBLIC_ENV_LINE = re.compile(
    r"^\s*(?:export\s+)?(NEXT_PUBLIC_[A-Za-z0-9_]+)\s*=", re.MULTILINE
)


def _next_public_keys_in_env_file(path: Path) -> list[str]:
    """NEXT_PUBLIC_* var names assigned in a dotenv-style file (empty if absent)."""
    if not path.exists():
        return []
    return _NEXT_PUBLIC_ENV_LINE.findall(path.read_text())


def ensure_no_frontend_env_overrides(ctx: ConvergeCtx) -> None:
    """Block any NEXT_PUBLIC_* build-time override that could poison the bundle.

    Two sources are scanned, both of which `next build` would inline into the JS
    bundle, silently beating the runtime gateway inference (ui/web/src/lib/api.ts):

    - Forbidden env FILES under `ui/web/` (`.env.production` etc.) — `next build`
      loads them directly.
    - NEXT_PUBLIC_* lines in the unit `$AVA_HOME/.env` — `load_ava_env` loads the
      WHOLE unit .env into os.environ at process start, so a stray
      NEXT_PUBLIC_GATEWAY_PORT there leaks into the build subprocess env (and into
      every service session's inherited env, where a watchdog respawn picks it
      up again). These
      vars belong nowhere in the unit .env: NEXT_PUBLIC_GATEWAY_PORT is derived
      from AVA_GATEWAY_PORT and injected on the build command line
      (shared.cluster.fe_build_env), never read from .env.
    """
    frontend = ctx.repo / "ui" / "web"
    present = [name for name in _FORBIDDEN_FRONTEND_ENV_FILES if (frontend / name).exists()]
    if present:
        raise RuntimeError(
            f"build-time env override(s) under {frontend}: {', '.join(present)} — "
            "these bake NEXT_PUBLIC_* into the frontend bundle and override the "
            "runtime gateway inference (ui/web/src/lib/api.ts). Move them aside "
            "(e.g. add a .bak suffix) and rerun."
        )
    unit_env = ctx.ava_home / ".env"
    leaked = _next_public_keys_in_env_file(unit_env)
    if leaked:
        raise RuntimeError(
            f"NEXT_PUBLIC_* override(s) in {unit_env}: {', '.join(leaked)} — the whole "
            "unit .env loads into os.environ at process start, so these bake into the "
            "frontend bundle (and every service session's inherited env) and override the runtime "
            "gateway inference (ui/web/src/lib/api.ts). NEXT_PUBLIC_GATEWAY_PORT is "
            "derived from AVA_GATEWAY_PORT and injected on the build command line — it "
            "must not live in .env. Delete the NEXT_PUBLIC_* line(s) and rerun."
        )
