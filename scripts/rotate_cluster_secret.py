#!/usr/bin/env python3
"""Emergency rotation for the control-plane bearer only.

``AVA_CLUSTER_SECRET`` authorizes gateway API, bootstrap, ops, and machine
registration. It intentionally no longer changes Postgres, Redis, their ACLs,
or PgBouncer. Routine data-plane rotation is ``rotate_data_plane_secrets.py``;
use this script only after a bearer leak and restart the gateway after it writes
the new local value.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from shared.config import settings
from shared.envfile import upsert_env
from shared.paths import ava_home

_TOKEN_BYTES = 32


@dataclass
class RotationState:
    """Recoverable bearer-rotation state, written 0600 because it holds both
    bearer values until every runner has been updated."""

    old_secret: str
    new_secret: str
    gateway_url: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    phase: str = "minted"

    def path(self) -> Path:
        stamp = self.started_at.replace(":", "").replace("+00:00", "Z")
        return ava_home() / "backups" / "secret-rotation" / f"bearer-{stamp}.json"

    def save(self) -> Path:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)
        return path

    @classmethod
    def load(cls, path: Path) -> RotationState:
        return cls(**json.loads(path.read_text()))


def mint_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def build_state(new_secret: str | None = None) -> RotationState:
    secret = settings.data_plane.cluster_secret
    if not secret:
        raise RuntimeError("this is a no-auth cluster; there is no bearer to rotate")
    gateway_url = settings.gateway.gateway_url.strip()
    if not gateway_url:
        raise RuntimeError("AVA_GATEWAY_URL is required to preflight bearer rotation")
    return RotationState(
        old_secret=secret,
        new_secret=new_secret or mint_secret(),
        gateway_url=gateway_url.rstrip("/"),
    )


def _bootstrap_status(gateway_url: str, bearer: str) -> int:
    response = httpx.get(
        f"{gateway_url}/api/bootstrap",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=5.0,
    )
    return response.status_code


def preflight(state: RotationState) -> bool:
    """Prove the current bearer succeeds and an unrelated bearer is rejected."""
    current = _bootstrap_status(state.gateway_url, state.old_secret)
    invalid = _bootstrap_status(state.gateway_url, secrets.token_urlsafe(_TOKEN_BYTES))
    print(f"  {'✓' if current == 200 else '✗'} current bearer GET /api/bootstrap ({current})")
    print(f"  {'✓' if invalid == 401 else '✗'} invalid bearer rejected ({invalid})")
    return current == 200 and invalid == 401


def _runner_roster() -> list[tuple[str, str | None]]:
    try:
        from shared.machines import list_agent_runners

        return list_agent_runners()
    except Exception as exc:
        print(f"  (could not read enrolled-runner roster: {exc})", file=sys.stderr)
        return []


def print_plan(state: RotationState, *, dry_run: bool) -> None:
    print("scope:             control-plane bearer only")
    print(f"gateway:           {state.gateway_url}")
    print(f"mode:              {'DRY RUN (read-only)' if dry_run else 'EXECUTE'}")
    runners = _runner_roster()
    print(f"\nenrolled agent-runners ({len(runners)}) — push AVA_CLUSTER_SECRET out of band:")
    for name, url in runners:
        print(f"  - {name} ({url or 'no URL on record'})")
    if not runners:
        print("  (none on record)")


def write_env(state: RotationState) -> None:
    """Stage the new bearer locally. Data-plane credentials are untouched; restart
    this gateway before attempting the new bearer."""
    upsert_env(
        ava_home() / ".env",
        {"AVA_CLUSTER_SECRET": state.new_secret},
        audit_site="rotate_cluster_secret",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emergency rotation for AVA_CLUSTER_SECRET.")
    parser.add_argument("--execute", action="store_true", help="perform the rotation")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--resume", metavar="STATE_FILE", help="resume a saved rotation")
    args = parser.parse_args(argv)

    state = RotationState.load(Path(args.resume)) if args.resume else build_state()
    print_plan(state, dry_run=not args.execute)
    if not args.execute:
        preflight(state)
        print("\n[dry-run] no changes made. This script never rotates data-plane credentials.")
        return 0
    if not args.resume and not preflight(state):
        print("\n✗ refusing to rotate an unverified bearer.", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input("\nType 'rotate bearer' to stage the new control-plane bearer: ")
        if answer.strip() != "rotate bearer":
            print("aborted.")
            return 1

    try:
        write_env(state)
        state.phase = "env_written"
        state_path = state.save()
    except Exception as exc:
        state_path = state.save()
        print(f"\n✗ bearer rotation failed at {state.phase!r}: {exc}", file=sys.stderr)
        print(f"  resume with --execute --resume {state_path}", file=sys.stderr)
        return 1

    print(f"\n✓ staged the new bearer in this gateway .env (recovery state: {state_path})")
    print("NEXT: restart this gateway, then push AVA_CLUSTER_SECRET to every enrolled runner")
    print("and restart each runner. Data-plane passwords and URLs were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
