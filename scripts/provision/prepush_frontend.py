from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_WEB = "ui/web/"
_CODE = (".ts", ".tsx", ".js", ".mjs", ".mts")
# meiva:ref scripts/provision/docs/prepush-selection.meiva.okf.md#type-artifact-inputs
_TYPE_INPUTS = {"ui/web/openapi.json", "ui/web/src/lib/contracts/types-generated.ts"}
_GLOBAL = {
    "ui/web/package.json",
    "ui/web/package-lock.json",
    "ui/web/tsconfig.json",
    "ui/web/tests/vitest.config.mts",
    "ui/web/tests/vitest.flaky.config.mts",
    "ui/web/tests/vitest.setup.ts",
    "ui/web/eslint.config.mjs",
    "ui/web/next.config.ts",
    "ui/web/postcss.config.mjs",
}
# meiva:ref scripts/provision/docs/prepush-selection.meiva.okf.md#disk-consumer-inputs
_DISK_CONSUMERS = (
    ("ui/web/src/", "src/lib/auth/localstorage-policy.test.ts"),
    ("ui/web/messages/", "src/i18n/messages-layout.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/globals-font-stack.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/color-scheme.test.ts"),
    ("ui/web/src/app/layout.tsx", "src/app/color-scheme.test.ts"),
    ("ui/web/package.json", "src/lib/transport/frontend-bind.test.ts"),
    ("ui/app/app-ui/", "src/app/app-ui-locale.test.ts"),
    ("tests/fixtures/events/", "src/lib/contracts/event-fixtures.test.ts"),
    ("services/entrypoints/gate/static/login.html", "src/lib/transport/gate-login.test.ts"),
    (
        "base/packages/plugins/surface/ui_contributions.py",
        "src/components/plugins/plugin-nav-icon.test.ts",
    ),
)


# meiva:ref scripts/provision/docs/prepush-selection.meiva.okf.md#fixed-command-execution
def _query(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


# meiva:ref scripts/provision/docs/prepush-selection.meiva.okf.md#branch-contribution-scope
def contribution() -> list[str]:
    base = subprocess.run(
        ["bash", str(_SCRIPTS / "hooks" / "prepush-base.sh")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return _query("diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")[:-1]


def _run(tool: str, command: list[str]) -> int:
    return subprocess.run(
        ["bash", str(_SCRIPTS / "hooks" / "prepush-guard.sh"), tool, "--", *command], check=False
    ).returncode


# meiva:ref scripts/provision/docs/prepush-selection.meiva.okf.md#empty-related-collection-is-not-green
def _vitest(paths: list[str], *, related: bool = False) -> int:
    return _run(
        "vitest",
        [
            "bash",
            "-c",
            'cd ui/web && exec npx --no-install vitest "$@"',
            "vitest",
            *(["related", "--run"] if related else ["run"]),
            "--config",
            "tests/vitest.config.mts",
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
            'cd ui/web && npx --no-install eslint --no-warn-ignored --max-warnings 0 "$@"',
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
