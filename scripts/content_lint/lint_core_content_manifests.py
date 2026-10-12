#!/usr/bin/env python3
"""Lint: core-content manifests — declared gates must hold against this repo.

Every manifest (`ava-plugin.json`) shipped under `ava_builtins/` is core
content: once the content channel delivers it (tasks #2915 / #3267), the gates
it declares are checked against the *derived* host version (the commit date of
the checkout it runs on, design §5.5 v3). CI is where that stays honest.

Hard checks (exit 1):
1. every builtin plugin (a tracked `ava_builtins/plugins/<name>/plugin.py`)
   ships a tracked `ava-plugin.json` beside it;
2. every core-content manifest validates (the same validator the install and
   refresh paths use);
3. `engines.ava` (when declared) admits this checkout's derived host version —
   a range that excludes the repo's own current version would refuse the very
   content that ships it;
4. `requires_commit` (when declared) is an ancestor of this checkout's HEAD —
   a typo'd or future SHA is red.

Audit (report-only, exit 0): every OTHER tracked manifest in the tree is
reported with its ranges judged against the derived host version.
`--audit-dir` extends the audit to machine-local manifests (evidence runs; not
used by CI).

Run: python3 scripts/content_lint/lint_core_content_manifests.py [--audit-dir DIR ...] (an
`--audit-dir` that does not exist is an error (stderr + exit 1) rather than a
silent skip; unrecognized arguments and a bare `--audit-dir` are rejected with
exit 2).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.host.git import host_version
from base.host.proc import run_bounded
from base.packages.plugins import manifest as pm

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CORE_ROOT = "ava_builtins"
_PLUGINS_ROOT = "ava_builtins/plugins"


def _tracked(pattern: str, rel_dir: str | None = None) -> list[Path]:
    """Tracked files matching the `git ls-files` `pattern` (optionally under `rel_dir`).

    Tracked-only via `git ls-files`: an untracked scratch manifest must not
    redden the gate, and a fresh checkout has no build artifacts to skip.
    """
    try:
        result = run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", pattern],
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


def _tracked_manifests(rel_dir: str | None = None) -> list[Path]:
    """Tracked `ava-plugin.json` files (optionally under `rel_dir`)."""
    return _tracked("*ava-plugin.json", rel_dir)


def _ranges_verdict(manifest: pm.PluginManifest, host: str) -> str:
    """PASS/FAIL for one manifest's declared gates against `host`."""
    problems = pm.check_host_engine(manifest, host)
    if problems:
        return f"FAIL ({'; '.join(problems)})"
    return "PASS"


def _audit_one(path: Path, derived: str) -> None:
    try:
        rel = path.relative_to(_REPO_ROOT)
    except ValueError:  # --audit-dir: a machine-local path outside the repo
        rel = path
    try:
        manifest = pm.load_manifest(path.parent)
    except pm.ManifestError as e:
        print(f"  audit {rel}: invalid manifest — {e}")
        return
    engines = manifest.engines or {}
    print(
        f"  audit {rel}: engines={engines or '-'} requires_commit="
        f"{manifest.requires_commit or '-'} | derived {derived}: {_ranges_verdict(manifest, derived)}"
    )


def _parse_audit_dirs(argv: list[str]) -> tuple[list[Path], int | None]:
    """The `--audit-dir` values, or the exit code of a usage / missing-path error."""
    audit_dirs: list[Path] = []
    while argv and argv[0] == "--audit-dir":
        argv.pop(0)
        if not argv:
            print("error: --audit-dir requires a value", file=sys.stderr)
            return [], 2
        audit_dirs.append(Path(argv.pop(0)).expanduser())

    missing = [str(d) for d in audit_dirs if not d.exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return [], 1
    if argv:
        print(f"error: unrecognized argument(s): {' '.join(argv)}", file=sys.stderr)
        return [], 2
    return audit_dirs, None


def _missing_plugin_manifests() -> int:
    """Report each tracked builtin plugin without a tracked manifest; the count."""
    root = _REPO_ROOT / _PLUGINS_ROOT
    manifests = {p.parent for p in _tracked_manifests(_PLUGINS_ROOT)}
    missing = 0
    for plugin_py in _tracked("*plugin.py", _PLUGINS_ROOT):
        plugin_dir = plugin_py.parent
        if plugin_py.name != "plugin.py" or plugin_dir.parent != root:
            continue
        if plugin_dir not in manifests:
            print(f"{plugin_dir.relative_to(_REPO_ROOT)}: builtin plugin ships no ava-plugin.json")
            missing += 1
    return missing


def _core_errors(derived: str) -> int:
    """Check every tracked core manifest against the derived host version; the error count."""
    errors = _missing_plugin_manifests()
    for path in _tracked_manifests(_CORE_ROOT):
        rel = path.relative_to(_REPO_ROOT)
        try:
            manifest = pm.load_manifest(path.parent)
        except pm.ManifestError as e:
            print(f"{rel}: invalid manifest — {e}")
            errors += 1
            continue
        for problem in pm.check_host_engine(manifest, derived):
            print(f"{rel}: {problem} (host version is the derived {derived})")
            errors += 1
        for problem in pm.check_host_commit(manifest, _REPO_ROOT):
            print(f"{rel}: {problem}")
            errors += 1
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    audit_dirs, failure = _parse_audit_dirs(argv)
    if failure is not None:
        return failure

    try:
        derived = host_version.host_version(_REPO_ROOT)
    except host_version.HostVersionError as e:
        print(f"cannot determine the repo's derived host version: {e}", file=sys.stderr)
        return 1

    errors = _core_errors(derived)

    audit = [p for p in _tracked_manifests() if not p.is_relative_to(_REPO_ROOT / _CORE_ROOT)]
    for d in audit_dirs:
        if d.is_dir():
            audit.extend(sorted(d.rglob("ava-plugin.json")))
    if audit:
        print(f"audit: {len(audit)} non-core manifest(s) against derived {derived}:")
        for path in audit:
            _audit_one(path, derived)

    if errors:
        print(f"\n{errors} core-content manifest error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
