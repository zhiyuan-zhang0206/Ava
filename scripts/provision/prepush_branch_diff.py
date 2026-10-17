from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TextIO, cast

_HOOK_ID = re.compile(r"^- hook id: (\S+)$")
_FAILURE = re.compile(r"^- (?:exit code: \d+|files were modified by this hook)$")
_TOOLCHAIN = (".venv", "ui/web/node_modules")


class SkipError(Exception):
    pass


def _repository() -> Path:
    query = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if query.returncode:
        raise SkipError(f"not inside a Git checkout: {query.stderr.strip()}")
    return Path(query.stdout.strip())


def _checkout(root: Path) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="meiva-branch-diff-"))
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", "--quiet", str(directory), "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        shutil.rmtree(directory, ignore_errors=True)
        raise SkipError(f"cannot check out HEAD into {directory}: {added.stderr.strip()}")
    for relative in _TOOLCHAIN:
        source = root / relative
        if source.exists():
            (directory / relative).symlink_to(source, target_is_directory=True)
    return directory


def _discard(root: Path, directory: Path) -> None:
    dropped = subprocess.run(
        ["git", "worktree", "remove", "--force", str(directory)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if dropped.returncode:
        shutil.rmtree(directory, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "prune"], cwd=root, capture_output=True, text=True, check=False
    )


def _restore(directory: Path) -> None:
    subprocess.run(
        ["git", "checkout", "--quiet", "--", "."],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def _modified(directory: Path) -> list[str]:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line[3:] for line in status.stdout.splitlines() if line.strip()]


def _run(
    root: Path, directory: Path, base: str, hooks: tuple[str, ...], *, stream: bool
) -> tuple[bool, str]:
    command = [
        str(root / ".venv/bin/pre-commit"),
        "run",
        "--color",
        "never",
        *hooks,
        "--hook-stage",
        "pre-commit",
        "--from-ref",
        base,
        "--to-ref",
        "HEAD",
    ]
    process = subprocess.Popen(
        command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    output: list[str] = []
    for line in cast(TextIO, process.stdout):
        output.append(line)
        if stream:
            sys.stdout.write(line)
            sys.stdout.flush()
    return process.wait() == 0, "".join(output)


def _failed_hooks(output: str) -> list[str]:
    found: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        identified = _HOOK_ID.match(line)
        if identified is not None:
            current = identified[1]
        elif _FAILURE.match(line) and current is not None:
            if current not in found:
                found.append(current)
            current = None
    return found


def _report(survivors: list[tuple[str, list[str], bool]], rewritten: list[str], base: str) -> None:
    print(f"branch diff lint: {len(survivors)} hook(s) failed over {base}..HEAD.", file=sys.stderr)
    for hook, files, exited in survivors:
        if not files:
            detail = "failed"
        elif exited:
            detail = f"failed and rewrote {len(files)} file(s): {', '.join(files)}"
        else:
            detail = f"rewrote {len(files)} file(s): {', '.join(files)}"
        print(f"  {hook}: {detail}", file=sys.stderr)
    if rewritten:
        print(
            f"note: {', '.join(rewritten)} failed in the first pass only because another hook "
            "rewrote files in the scratch checkout; against HEAD's text it passes.",
            file=sys.stderr,
        )
    print(
        "these hooks ran in a throwaway checkout of HEAD; your working tree was not modified.",
        file=sys.stderr,
    )


def _judge(root: Path, directory: Path, base: str) -> int:
    passed, output = _run(root, directory, base, (), stream=True)
    if passed:
        return 0
    failed = _failed_hooks(output)
    if not failed:
        print(
            f"branch diff lint: the run over {base}..HEAD failed without naming a hook; "
            "see the output above.",
            file=sys.stderr,
        )
        return 1
    survivors: list[tuple[str, list[str], bool]] = []
    rewritten: list[str] = []
    for hook in failed:
        _restore(directory)
        hook_passed, hook_output = _run(root, directory, base, (hook,), stream=False)
        files = _modified(directory)
        if hook_passed:
            rewritten.append(hook)
        else:
            survivors.append((hook, files, "- exit code:" in hook_output))
    if survivors:
        _report(survivors, rewritten, base)
        return 1
    print(
        f"branch diff lint: {', '.join(rewritten)} failed over {base}..HEAD only because "
        "another hook rewrote files; against HEAD's text it passes.",
        file=sys.stderr,
    )
    print("these hooks ran in a throwaway checkout of HEAD.", file=sys.stderr)
    return 0


def _scope(root: Path, pushed: str) -> None:
    if not pushed:
        return
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    if head and pushed != head:
        print(
            f"warning: the branch-diff gate judges the checkout's HEAD ({head[:9]}); "
            f"the pushed ref is {pushed[:9]}.",
            file=sys.stderr,
        )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: prepush_branch_diff.py <base-sha> [<pushed-sha>]", file=sys.stderr)
        return 2
    base = argv[1]
    pushed = argv[2] if len(argv) == 3 else ""
    try:
        root = _repository()
        directory = _checkout(root)
    except SkipError as error:
        print(
            f"WARNING: PRE-PUSH SKIPPED [branch-lint]: {error}. CI must pass before merge.",
            file=sys.stderr,
        )
        return 0
    try:
        verdict = _judge(root, directory, base)
        _scope(root, pushed)
        return verdict
    finally:
        _discard(root, directory)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
