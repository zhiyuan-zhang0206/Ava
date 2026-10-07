"""Run frontend checks over the branch contribution, never an old push tip.

Vitest's import graph is only one source of consumers. Keep existing filesystem
contract tests explicit; global configuration and deleted-module closure belong
to CI and are reported as unverified locally, rather than running a full suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_WEB = "ui/web/"
_CODE = (".ts", ".tsx", ".js", ".mjs", ".mts")
# Canonical HTTP schema/type artifacts have no runtime imports. Their consumers
# are checked by project tsc and the existing codegen-freshness hook.
_TYPE_INPUTS = {"ui/web/openapi.json", "ui/web/src/lib/types-generated.ts"}
_GLOBAL = {
    "ui/web/package.json",
    "ui/web/package-lock.json",
    "ui/web/tsconfig.json",
    "ui/web/vitest.config.mts",
    "ui/web/vitest.flaky.config.mts",
    "ui/web/vitest.setup.ts",
    "ui/web/eslint.config.mjs",
    "ui/web/next.config.ts",
    "ui/web/postcss.config.mjs",
    "ui/web/scripts/eslint-warning-baseline.json",
}
# Consumers read these inputs from disk, outside Vitest's import graph.
_DISK_CONSUMERS = (
    ("ui/web/src/", "src/lib/localstorage-policy.test.ts"),
    ("ui/web/messages/", "src/i18n/messages-layout.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/globals-font-stack.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/color-scheme.test.ts"),
    ("ui/web/src/app/layout.tsx", "src/app/color-scheme.test.ts"),
    ("ui/web/package.json", "src/lib/frontend-bind.test.ts"),
    ("ui/app/app-ui/", "src/app/app-ui-locale.test.ts"),
    ("tests/fixtures/events/", "src/lib/event-fixtures.test.ts"),
    ("services/entrypoints/gate/static/login.html", "src/lib/gate-login.test.ts"),
    ("base/packages/plugins/ui_contributions.py", "src/components/plugin-nav-icon.test.ts"),
    ("ui/web/scripts/fixtures/route-bundle-stats.json", "scripts/check-first-load-js.test.ts"),
)


def _query(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed Git queries
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def contribution() -> list[str]:
    """Both sides of renames and deletions count; unknown scope is an error."""
    base = subprocess.run(  # noqa: S603 — fixed repo-local scope owner
        ["bash", str(_SCRIPTS / "hooks" / "prepush-base.sh")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return _query("diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")[:-1]


def _run(tool: str, command: list[str]) -> int:
    return subprocess.run(  # noqa: S603 — fixed command, Git-owned path arguments
        ["bash", str(_SCRIPTS / "hooks" / "prepush-guard.sh"), tool, "--", *command], check=False
    ).returncode


def _vitest(paths: list[str], *, related: bool = False) -> int:
    # `related` defaults passWithNoTests=true in Vitest itself. Override it:
    # empty affected collection is missing evidence, never a passing test run.
    return _run(
        "vitest",
        [
            "bash",
            "-c",
            'cd ui/web && exec npx --no-install vitest "$@"',
            "vitest",
            *(["related", "--run"] if related else ["run"]),
            "--passWithNoTests=false",
            *paths,
        ],
    )


def _eslint(existing: list[str]) -> int:
    files = [p.removeprefix(_WEB) for p in existing if p.endswith(_CODE)]
    if not files:
        print(
            "WARNING: PRE-PUSH UNVERIFIED [eslint]: no surviving code paths; global/deletion effects require CI",
            file=sys.stderr,
        )
        return 0
    return _run(
        "eslint",
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            'cd ui/web && npx --no-install eslint --no-warn-ignored --format json "$@" | node scripts/check-eslint-warnings.mjs',
            "eslint",
            *files,
        ],
    )


def _tests(web: list[str], existing: list[str], disk: list[str]) -> int:
    if any(p in _GLOBAL or not Path(p).is_file() for p in web):
        print(
            "WARNING: PRE-PUSH UNVERIFIED [vitest]: global configuration/deleted-input closure requires CI",
            file=sys.stderr,
        )
    direct = _direct_tests(existing, disk)
    sources = _related_sources(existing, direct)
    if direct:
        status = _vitest(direct)
        if status:
            return status
    if sources:
        return _vitest(sources, related=True)
    if not direct:
        if existing and all(p in _TYPE_INPUTS for p in existing):
            print(
                "pre-push: schema/type artifacts require tsc and codegen freshness; no runtime Vitest input"
            )
            return 0
        print(
            "WARNING: PRE-PUSH UNVERIFIED [vitest]: no locally selectable consumer; affected verification requires CI",
            file=sys.stderr,
        )
    return 0


def _direct_tests(existing: list[str], disk: list[str]) -> list[str]:
    return sorted(
        set(disk) | {p.removeprefix(_WEB) for p in existing if ".test." in p or ".spec." in p}
    )


def _related_sources(existing: list[str], direct: list[str]) -> list[str]:
    return [
        p.removeprefix(_WEB)
        for p in existing
        if p not in _GLOBAL
        and p not in _TYPE_INPUTS
        and (p.endswith(_CODE) or p.endswith(".json"))
        and p.removeprefix(_WEB) not in direct
    ]


def main(tool: str) -> int:
    paths = contribution()
    web = [p for p in paths if p.startswith(_WEB)]
    disk = sorted(
        {test for prefix, test in _DISK_CONSUMERS if any(p.startswith(prefix) for p in paths)}
    )
    if not web and (tool != "vitest" or not disk):
        print(f"pre-push: no {tool} input in branch contribution; no tool invoked")
        return 0
    if tool == "tsc":
        return _run(
            tool,
            [
                "bash",
                "-c",
                "cd ui/web && npx --no-install next typegen && npx --no-install tsc --noEmit",
            ],
        )
    existing = [p for p in web if Path(p).is_file()]
    if tool == "eslint":
        return _eslint(existing)
    return _tests(web, existing, disk)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"tsc", "eslint", "vitest"}:
        raise SystemExit("usage: prepush_frontend.py tsc|eslint|vitest")
    try:
        raise SystemExit(main(sys.argv[1]))
    except subprocess.CalledProcessError as error:
        print(error.stderr or str(error), file=sys.stderr)
        raise SystemExit(error.returncode) from None
