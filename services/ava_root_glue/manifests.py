"""Generate K2 unit manifests from the deployment roster (W1.2e).

The generator is the deployment-side half of the root supervisor: it reads
the ops roster (`ops.roster.build_services`) — the machine-readable place
that says which service runs on which capability set — and renders the K2
manifest JSON the root consumes (`{"units": [...]}`; schema and validators in
`services.ava_root.manifest`). Nothing here spawns or stops anything: this is
the "prepare the tree's shape" slice (W1.2e), the swap itself is W1.2e-2.

Mapping semantics (W1.1 v1 row set + G6 rulings):

- every roster spec becomes a unit: id = `spec.session`, exec = the spec's
  shell command (the same string a session runs, prefixed `cd <repo>`),
  restart = always (the W1.1 default), attach = root (static services);
- the per-(machine x home) subset is `spec.capabilities & target
  capabilities` — the single readable place "which machine runs this";
- application health recovery belongs to root's health monitor; there are
  no watchdog service rows;
- session-host classes (interactive shells / watchers / page servers /
  schedule runners / orchestration sessions) spawn at RUNTIME via their
  owning service (E2a) — they are not static manifest rows; their terminal
  attach values (G6b) live in `SESSION_HOST_ATTACH` for the spawn slice;
- OS-edge / root-internal / retired rows never appear (W1.1 D/E/F).

Development exec derivation: a spec's `cmd` is a shell command, so the unit
argv is `/bin/sh -c 'cd <repo> && ...'`. When the command is a simple one
(bare tokens only, no leading assignment), an `exec` prefix replaces the
shell with the service itself, keeping the unit's pid a direct child of root
(chain discipline, I2); a compound command keeps its own shape (the frontend
command already ends in `exec npm ...`).

An explicitly admitted release uses direct argv under the captured image cwd.
Its executable and Python module must belong to the image; Python argv retains
`-I -B -X utf8`, and shell compound commands are rejected before root launch.

Output is always validated BEFORE anything consumes it — in memory via the
same `UnitManifest.from_mapping` validators `load_manifests` uses, and again
by re-reading the written file with `load_manifests` itself (the fail-fast
self-check the W1.2e brief pins).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast, get_args

from ops.roster import build_services
from ops.service_spec import ServiceSpec
from services.ava_root.inputs import InputSeal
from services.ava_root.manifest import (
    ManifestError,
    UnitManifest,
    UnitRegistry,
    load_manifests,
)
from shared.machine import MachineRole
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease

_REPO_ROOT = Path(__file__).resolve().parents[2]

_KNOWN_CAPABILITIES = frozenset(get_args(MachineRole))

# G6b attach values for classes that spawn at RUNTIME (never static manifest
# rows — see the module docstring). Kept here so the spawn slice (W1.3) has
# one code-side source instead of re-reading the ruling.
SESSION_HOST_ATTACH: dict[str, str] = {
    "agent-shell": "agent-host",
    "watcher-session": "agent-host",
    "page-server-instance": "page-server",
    "schedule-runner": "gateway",
    "orchestration-session": "ops",
    "exec-child": "agent-host",
}

# A command is "simple" when the shell adds nothing: bare tokens only. The
# `exec` prefix then replaces the shell with the service itself (chain
# discipline, I2). Anything with metacharacters, quoting, a leading
# assignment, or a command already starting with `exec` keeps the plain form
# (never `exec exec ...`).
_SHELL_WORD = r"[A-Za-z0-9_./@%+:,=^-]+"
_SIMPLE_COMMAND_RE = re.compile(rf"{_SHELL_WORD}(?: {_SHELL_WORD})*\Z")


def _exec_argv(cmd: str, repo_root: Path) -> list[str]:
    """The unit argv for a roster command: `cd <repo> && [exec] <cmd>`."""
    if sys.platform == "win32":
        argv = shlex.split(cmd)
        if not argv or any(token in {"&&", "||", "|", ";", ">", "<"} for token in argv):
            raise ManifestError("Windows application manifests require a direct command")
        if argv[0] == ".venv/bin/python":
            argv[0] = str(repo_root / ".venv" / "Scripts" / "python.exe")
        if not Path(argv[0]).is_absolute():
            raise ManifestError("Windows application executable must be absolute")
        return argv
    cd = f"cd {shlex.quote(str(repo_root))}"
    first = cmd.split(" ", 1)[0]
    if _SIMPLE_COMMAND_RE.match(cmd) and "=" not in first and first != "exec":
        return ["/bin/sh", "-c", f"{cd} && exec {cmd}"]
    return ["/bin/sh", "-c", f"{cd} && {cmd}"]


def _direct_command(cmd: str) -> tuple[list[str], dict[str, str]]:
    tokens = shlex.split(cmd)
    environment: dict[str, str] = {}
    while tokens and "=" in tokens[0]:
        key, value = tokens.pop(0).split("=", 1)
        if key not in {"NODE_ENV", "HOSTNAME", "PORT"}:
            raise ReleaseRejectedError("release command has an unsupported environment prefix")
        environment[key] = value
    if tokens and tokens[0] == "exec":
        tokens.pop(0)
    if not tokens or any(token in {";", "&&", "||", "|", ">", "<", "&"} for token in tokens):
        raise ReleaseRejectedError("release service requires a direct executable command")
    return tokens, environment


def _release_command(
    cmd: str, image: VerifiedRelease, package: Path
) -> tuple[list[str], dict[str, str]]:
    """Admit direct image commands; never interpret a release command in a shell."""
    tokens, environment = _direct_command(cmd)
    executable = Path(tokens[0])
    if executable == Path(".venv/bin/python"):
        if len(tokens) < 3 or tokens[1] != "-m":
            raise ReleaseRejectedError("release Python service requires a module entry point")
        tokens = list(image.module_argv(tokens[2], *tokens[3:]))
        executable = image.interpreter
    if not executable.is_absolute() or not executable.resolve(strict=True).is_relative_to(
        image.root
    ):
        raise ReleaseRejectedError("release executable escapes the verified image")
    if executable == image.interpreter:
        _require_release_module(tokens, image, package)
    elif "-m" in tokens:
        raise ReleaseRejectedError("release module requires the captured interpreter")
    return tokens, environment


def _require_release_module(tokens: list[str], image: VerifiedRelease, package: Path) -> None:
    if len(tokens) < 7 or tokens[:6] != [str(image.interpreter), "-I", "-B", "-X", "utf8", "-m"]:
        raise ReleaseRejectedError("release Python service requires captured -I -B module argv")
    module = tokens[6]
    image.module_argv(module)  # Validate the module grammar before resolving its retained file.
    relative = Path(*module.split("."))
    members = [package / relative.with_suffix(".py"), package / relative / "__main__.py"]
    found = [path for path in members if path.is_file()]
    if len(found) != 1 or not found[0].resolve(strict=True).is_relative_to(image.root):
        raise ReleaseRejectedError("release module has no unique retained entry point")


def _validated_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    caps = frozenset(capabilities)
    if not caps:
        raise ManifestError("capabilities must not be empty")
    unknown = caps - _KNOWN_CAPABILITIES
    if unknown:
        raise ManifestError(
            f"unknown capability token(s) {sorted(unknown)}; known: {sorted(_KNOWN_CAPABILITIES)}"
        )
    return caps


def build_units(
    specs: Iterable[ServiceSpec] | None = None,
    *,
    capabilities: Iterable[str],
    repo_root: Path,
    environments: Mapping[str, dict[str, str]] | None = None,
    release: VerifiedRelease | None = None,
) -> list[dict[str, object]]:
    """K2 unit rows for every spec whose capabilities intersect `capabilities`."""
    chosen = tuple(build_services()) if specs is None else tuple(specs)
    caps = _validated_capabilities(capabilities)
    units: list[dict[str, object]] = []
    for spec in chosen:
        if not (spec.capabilities & caps):
            continue
        argv, prefix_env = (
            (_exec_argv(spec.cmd, repo_root), {})
            if release is None
            else _release_command(spec.cmd, release, repo_root)
        )
        units.append(
            {
                "id": spec.session,
                "exec": argv,
                "restart": "always",
                "attach": "root",
                "env": prefix_env | ({} if environments is None else environments[spec.session]),
                "inputs": [InputSeal.capture(path).as_mapping() for path in spec.config_inputs],
            }
        )
    return units


def _validate(manifest: Mapping[str, object]) -> UnitRegistry:
    """Run the manifest validators in memory, before anything touches disk."""
    units_raw = manifest["units"]
    if not isinstance(units_raw, list):
        raise ManifestError("generated manifest: 'units' must be a list")
    parsed: list[UnitManifest] = []
    for index, item in enumerate(cast("list[object]", units_raw)):
        if not isinstance(item, dict):
            raise ManifestError(f"generated manifest: units[{index}] must be an object")
        parsed.append(
            UnitManifest.from_mapping(
                cast("dict[str, object]", item), origin=f"generated units[{index}]"
            )
        )
    return UnitRegistry(parsed)


def build_manifest(
    *,
    capabilities: Iterable[str],
    repo_root: Path | None = None,
    specs: Iterable[ServiceSpec] | None = None,
    environments: Mapping[str, dict[str, str]] | None = None,
    release: VerifiedRelease | None = None,
) -> dict[str, object]:
    """Build and validate a K2 manifest document (fail-fast, in memory).

    The capability set is an explicit input: this module must not read the
    local machine role (the gateway is the single routing point; role calls
    are allowlisted by scripts/lint_code_structure.py). The wiring slice
    supplies the target's set. `repo_root=None` resolves this checkout.
    """
    resolved_repo = _REPO_ROOT if repo_root is None else repo_root
    caps = _validated_capabilities(capabilities)
    manifest: dict[str, object] = {
        "units": build_units(
            specs,
            capabilities=caps,
            repo_root=resolved_repo,
            environments=environments,
            release=release,
        )
    }
    _validate(manifest)
    return manifest


def write_manifest(path: Path, manifest: Mapping[str, object]) -> Path:
    """Write the manifest JSON (validate with `build_manifest` first)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from shared.atomic_io import write_text_atomic

    write_text_atomic(path, json.dumps(manifest, indent=2) + "\n", mode=0o600, sync_parent=True)
    return path


def generate(
    path: Path,
    *,
    capabilities: Iterable[str],
    repo_root: Path | None = None,
    specs: Iterable[ServiceSpec] | None = None,
    environments: Mapping[str, dict[str, str]] | None = None,
) -> Path:
    """Build, validate in memory, write, then re-read with `load_manifests`."""
    manifest = build_manifest(
        capabilities=capabilities, repo_root=repo_root, specs=specs, environments=environments
    )
    write_manifest(path, manifest)
    load_manifests(path)  # the consumer's own reader must accept what we wrote
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: `python -m services.ava_root_glue.manifests --out FILE [...]`."""
    parser = argparse.ArgumentParser(
        prog="ava-root-manifests",
        description="Generate a K2 unit manifest JSON from the deployment roster (W1.2e).",
    )
    parser.add_argument("--out", required=True, type=Path, help="manifest JSON file to write")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="checkout root the unit commands cd into (default: this checkout)",
    )
    parser.add_argument(
        "--capabilities",
        required=True,
        help="comma-separated capability set to filter for (e.g. gateway,agent-runner)",
    )
    args = parser.parse_args(argv)
    caps = [part.strip() for part in args.capabilities.split(",") if part.strip()]
    try:
        path = generate(args.out, capabilities=caps, repo_root=args.repo_root)
    except ManifestError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    manifest = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    sys.stdout.write(f"wrote {len(cast('list[object]', manifest['units']))} unit(s) to {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
