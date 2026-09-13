#!/usr/bin/env python3
"""Lint: core-content manifests — declared gates must hold against this repo.

Every manifest (`ava-plugin.json`) shipped under `ava_builtins/` is core
content: once the content channel delivers it (tasks #2915 / #3267), the gates
it declares are checked against the *derived* host version (the commit date of
the checkout it runs on, design §5.5 v3). CI is where that stays honest.

Hard checks (exit 1):
1. every core-content manifest validates (the same validator the install and
   refresh paths use);
2. `engines.ava` (when declared) admits this checkout's derived host version —
   a range that excludes the repo's own current version would refuse the very
   content that ships it;
3. `requires_commit` (when declared) is an ancestor of this checkout's HEAD —
   a typo'd or future SHA is red.

Audit (report-only, exit 0): every OTHER tracked manifest in the tree is
reported with its ranges judged against the derived host version AND the
legacy pyproject version, so the derived-version switch in the shared install
gate (`cli/commands/_manifest_gate.py`) is auditable per PR: an old-style
min-only range keeps passing, and a legacy fixture that would be refused is
visible in the log instead of surfacing as a surprise later. `--audit-dir`
extends the audit to machine-local manifests (evidence runs; not used by CI).

Run: python3 scripts/lint_core_content_manifests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from shared import host_version
from shared import plugin_manifest as pm
from shared.proc import run_bounded

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CORE_ROOT = "ava_builtins"


def _tracked_manifests(rel_dir: str | None = None) -> list[Path]:
    """Tracked `ava-plugin.json` files (optionally under `rel_dir`).

    Tracked-only via `git ls-files`: an untracked scratch manifest must not
    redden the gate, and a fresh checkout has no build artifacts to skip.
    """
    try:
        result = run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "*ava-plugin.json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    out = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        if rel_dir is not None and not rel.startswith(rel_dir + "/"):
            continue
        out.append(_REPO_ROOT / rel)
    return out


def _ranges_verdict(manifest: pm.PluginManifest, host: str) -> str:
    """PASS/FAIL for one manifest's declared gates against `host`."""
    problems = pm.check_host_engine(manifest, host)
    if problems:
        return f"FAIL ({'; '.join(problems)})"
    return "PASS"


def _audit_one(path: Path, derived: str, legacy: str | None) -> None:
    try:
        rel = path.relative_to(_REPO_ROOT)
    except ValueError:  # --audit-dir: a machine-local path outside the repo
        rel = path
    try:
        manifest = pm.load_manifest(path.parent)
    except pm.ManifestError as e:
        print(f"  audit {rel}: invalid manifest — {e}")
        return
    if manifest is None:
        return
    derived_verdict = _ranges_verdict(manifest, derived)
    legacy_verdict = _ranges_verdict(manifest, legacy) if legacy else "n/a"
    engines = manifest.engines or {}
    print(
        f"  audit {rel}: engines={engines or '-'} requires_commit="
        f"{manifest.requires_commit or '-'} | derived {derived}: {derived_verdict}"
        f" | legacy {legacy or '-'}: {legacy_verdict}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    audit_dirs: list[Path] = []
    while argv and argv[0] == "--audit-dir":
        argv.pop(0)
        audit_dirs.append(Path(argv.pop(0)).expanduser())

    try:
        derived = host_version.host_version(_REPO_ROOT)
    except host_version.HostVersionError as e:
        print(f"cannot determine the repo's derived host version: {e}", file=sys.stderr)
        return 1
    try:
        legacy: str | None = pm.host_version_from_repo(_REPO_ROOT)
    except pm.ManifestError:
        legacy = None

    core = _tracked_manifests(_CORE_ROOT)
    errors = 0
    for path in core:
        rel = path.relative_to(_REPO_ROOT)
        try:
            manifest = pm.load_manifest(path.parent)
        except pm.ManifestError as e:
            print(f"{rel}: invalid manifest — {e}")
            errors += 1
            continue
        if manifest is None:
            continue
        for problem in pm.check_host_engine(manifest, derived):
            print(f"{rel}: {problem} (host version is the derived {derived})")
            errors += 1
        for problem in pm.check_host_commit(manifest, _REPO_ROOT):
            print(f"{rel}: {problem}")
            errors += 1

    audit = [p for p in _tracked_manifests() if not p.is_relative_to(_REPO_ROOT / _CORE_ROOT)]
    for d in audit_dirs:
        if d.is_dir():
            audit.extend(sorted(d.rglob("ava-plugin.json")))
    if audit:
        print(f"audit: {len(audit)} non-core manifest(s) against derived {derived}:")
        for path in audit:
            _audit_one(path, derived, legacy)

    if errors:
        print(f"\n{errors} core-content manifest error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
