#!/usr/bin/env python3
r"""Find docs that reference things which do not exist: CLI flags and file paths.

Run: `.venv/bin/python scripts/check_doc_references.py` (exit 1 on any hit).

## Why

Two escapes, hours apart, from the same class:

- `conventions/windows-setup.md` told readers to run
  `install.sh --skip-native-infra`. That flag has never existed.
- `ava_builtins/skills/ava-self-development/SKILL.md` — a skill agents load and
  *execute* — told them `ava start --cluster preview`, eight days after
  `--cluster` was removed. The same section still described cluster names coming
  from the worktree directory, the mechanism that once birthed a phantom cluster
  over prod's own.

`lint_doc_symbols.py` validates `ava.<name>` SDK references, so a deleted
submodule cannot rot in the docs. Nothing covered the two things docs assert most
often: **what you can type** and **what you can open**.

## A pre-commit hook (`check-doc-references`)

Runs the full doc tree on every commit and in CI (any PR touching a tracked
`.md` runs the whole suite, not just the changed file — a link anywhere can go
stale from a move anywhere else). ~0.1s over ~430 docs, so `--all-files` on
every job is free.

Three doc axes get exemptions, not a blanket skip:

- `decisions/` is skipped entirely (flags and links both). A decision
  record legitimately names the flag it removed or the file it deleted (see
  `decisions/2026-07-20-path-only-cluster-identity.md`) and history is
  never rewritten to satisfy a linter.
- `postmortems/` is skipped entirely, for the same reason: an incident
  narrative is frozen, and the code path, flag, or file it names is the one
  that existed when the failure escaped — often one that was deleted as part
  of the fix. Because nothing checks those links, the template requires
  unreachable anchors to be labelled (`(pre-cutover)`, `(summarized)`) in the
  prose.
- `future/` skips CLI-flag checks only — a plan may propose a flag that
  does not exist yet — but its relative links resolve like any other doc's. A
  link to a file that exists-but-moved is rot, not a plan (issue #1045: two
  such links shipped clean through the old blanket skip during the
  dev-skills conversion). A link that genuinely names something not yet
  built opts out with a `(planned)` marker immediately after it:
  `` [`ops/identity.py`](../ops/identity.py) (planned) ``.

## What it checks

1. **CLI flags.** Every `--flag` written as part of a command invocation,
   checked against the live source of truth: the argparse tree from
   `cli.main._build_parser` (plus `cli/enroll.py`, routed before the parser) and
   each `scripts/*.sh`'s own case arms.

   Three things this gets right that the obvious version does not, each a false
   negative found by review rather than by the tool going green:
   - Options are keyed by **command path**, not unioned. A flat set accepts
     `ava stop --machine-name` because `start` has that flag.
   - A shell **continuation** carries the command across lines, so the four
     flags under `ava start \` are validated rather than skipped.
   - A script's options come only from **case arms at line start**, so the
     `$(node --version)` inside an `echo` does not make `install.sh --version`
     look legal.

   Attribution happens only inside code — a fenced block, or one inline-code
   span. Prose is not an invocation: "`ava update` keeps it up (`--keep-infra`)"
   describes what update passes to its internal stop, and reading English words
   as arguments attributed that flag across a sentence.
2. **Relative links.** Every `[text](path)` in a tracked doc, resolved against
   the tree. Only real markdown links — a link is meant to be followed, so a
   dangling one is unambiguously a defect. Backticked paths are deliberately NOT
   checked: prose writes `memory/MEMORY.md` for a runtime directory, `<slug>.md`
   for a placeholder, `raw_path/post.json` for a JSON field, and a path relative
   to a sibling package for a co-located OKF. Those readings cannot be told apart
   from rot without guessing, and a checker that guesses gets ignored.

   Code samples are not links either, on all three shapes markdown offers:
   inline span, fenced block, and the four-space indented block. The memory
   plugin's `MEMORY.md` template teaches its pointer format with an indented
   sample — `- [Title](path.md) — …` — and `path.md` is the placeholder being
   taught, not a file anyone can open.

   In `future/`, a dangling link followed immediately by a `(planned)`
   marker is not reported — see "A pre-commit hook" above.
3. **Skill `references/` backtick refs.** A repo-shipped `SKILL.md`
   writes its reference-library pointers as backticked
   `` `references/<file>.md` `` — prose, not markdown links, so axis 2 skips
   them, yet unlike a runtime path they are unambiguous: the target lives in a
   `references/` dir of this skill or one of its ancestors. Three review
   rounds lost two of these to moves inside the library, so SKILL.md backtick
   refs are resolved against the nearest `references/` dir up the skill tree
   (a nested skill shares its parent's library). Placeholders (`<name>.md`)
   are skipped; everything else must resolve.

Attribution is a token walk, not a regex. Every pattern that expressed "a
command, then some words, then a flag" needed a quantifier next to whitespace,
and each one backtracked catastrophically on ordinary prose.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO))

_FLAG = re.compile(r"^(--[a-z0-9][a-z0-9-]*)")
_WORD = re.compile(r"^[a-z][a-z0-9_-]*$")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# Opt-out for a `future/` link that names something not yet built — must sit
# immediately after the link (only whitespace between), so it marks THIS link and
# cannot be misread as covering an unrelated dangling one later in the same line.
# No leading `^`: `.match(line, pos)` already anchors at `pos`, and `^` would
# instead demand `pos == 0` (it anchors to the true string start, not to
# wherever the search began) and never fire mid-line.
_PLANNED = re.compile(r"\s*\(planned\)", re.IGNORECASE)


def _options(parser: argparse.ArgumentParser) -> set[str]:
    out: set[str] = set()
    for action in parser._actions:
        out.update(s for s in action.option_strings if s.startswith("--"))
    return out


def ava_flags() -> dict[tuple[str, ...], set[str]]:
    """Options keyed by the command path that accepts them.

    Keyed rather than unioned: a flat set would accept `ava stop --machine-name`
    because `--machine-name` exists on `start`, which is the false negative this
    tool exists to avoid."""
    from cli.main import _build_parser

    parser = _build_parser()
    by_path: dict[tuple[str, ...], set[str]] = {("ava",): _options(parser)}
    stack: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = [(("ava",), parser)]
    while stack:
        path, current = stack.pop()
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    child = (*path, name)
                    by_path[child] = _options(sub)
                    stack.append((child, sub))
    # `ava enroll` is routed before the main parser (it must not import Settings),
    # so its options are not in the tree above.
    enroll_src = (REPO / "cli" / "enroll.py").read_text()
    by_path[("ava", "enroll")] = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', enroll_src))
    return by_path


# A case arm opens a line; `$(node --version)` inside an echo, and a `--role)` in
# help prose, do not. Anchoring keeps a script's own options apart from every
# other command it happens to run — otherwise `install.sh --version` validates.
_CASE_ARM = re.compile(r"^[ \t]*(--[a-z0-9][a-z0-9|=*-]*)\)", re.MULTILINE)


def shell_flags(script: Path) -> set[str]:
    """Long options a bash script's own argument-parsing arms match on."""
    src = script.read_text()
    flags: set[str] = set()
    for arm in _CASE_ARM.findall(src):
        # One arm may list alternatives (`--a|--b)`) and may take `=value`.
        for part in arm.split("|"):
            name = part.split("=", 1)[0]
            if name.startswith("--") and len(name) > 2:
                flags.add(name)
    flags |= set(re.findall(r'== ?"(--[a-z0-9][a-z0-9-]*)"', src))
    return flags


def tracked_docs() -> list[Path]:
    out = subprocess.run(  # noqa: S603 — fixed argv; REPO comes from git rev-parse, not input
        ["git", "-C", str(REPO), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / rel for rel in out]


def _command_segments(line: str, *, fenced: bool) -> list[str]:
    """The stretches of this line that are a command invocation, not prose.

    A command is written as code. Inside a fenced block the whole line is; in
    prose only inline-code spans are, and each span stands alone. Walking prose
    was the mistake: the runbook says "`ava update` keeps it up (`--keep-infra`)"
    to describe what update passes to its internal stop, and English words
    between the two kept the command path open long enough to attribute a flag
    across a sentence."""
    if fenced:
        return [line]
    return re.findall(r"`([^`]+)`", line)


def check_flags(
    line: str,
    commands: dict[tuple[str, ...], set[str]],
    carry: tuple[str, ...] | None,
    *,
    fenced: bool,
) -> tuple[list[tuple[str, str]], tuple[str, ...] | None]:
    r"""(bad (command, flag) pairs, the command path still open at end of line).

    `carry` is the path left open by the previous line's trailing backslash, so a
    multi-line invocation validates its continuation flags too — `ava start \`
    with four flags on the lines below is how the runbook writes it, and each of
    those lines carries no command token of its own. Only meaningful inside a
    fenced block; a backslash in prose continues nothing."""
    bad: list[tuple[str, str]] = []
    path: tuple[str, ...] | None = carry if fenced else None
    for segment in _command_segments(line, fenced=fenced):
        if not fenced:
            path = None  # each inline span is its own invocation
        for word in segment.replace("(", " ").replace(")", " ").split():
            if word == "\\":
                continue  # line-continuation marker, not an argument that ends the command
            name = word.rsplit("/", 1)[-1]
            if (name,) in commands:
                path = (name,)
                continue
            flag = _FLAG.match(word)
            if flag:
                if path is not None and flag.group(1) not in commands[path]:
                    bad.append((" ".join(path), flag.group(1)))
                continue
            if path is not None and (*path, word) in commands:
                path = (*path, word)  # a subcommand narrows which flags are legal
                continue
            # Punctuation, a URL, a pipe — the command's reach ends; an argument
            # value keeps it alive.
            if not _WORD.match(word):
                path = None
    open_next = path if (fenced and line.rstrip().endswith("\\")) else None
    return bad, open_next


def _in_code_span(line: str, pos: int) -> bool:
    """True when `pos` sits inside an inline-code span.

    A doc that explains markdown shows markdown: the ava-ui widget README writes
    ``` `![alt](images/1.jpg)` ``` to describe how image paths resolve. That is
    syntax being displayed, not a link to follow."""
    return line.count("`", 0, pos) % 2 == 1


_LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s")


def _indented_code(line: str, *, open_now: bool, prev_blank: bool, in_list: bool) -> bool:
    """Whether `line` sits inside a four-space indented code block.

    The other thing four spaces mean in markdown is list continuation, which is
    why `in_list` is part of the question rather than the indent alone: under a
    bullet, `    - nested` is a sub-item whose links are real; under a
    paragraph, the same text is a literal sample. The memory plugin's template
    is exactly the ambiguous case — it teaches its pointer format with an
    indented sample whose content *is* a bullet — so the line cannot be
    classified without the block it sits in.

    A block opens on an indented line that follows a blank one (markdown's rule:
    an indented block cannot interrupt a paragraph), stays open across blank
    lines, and closes at the first non-blank line that is not indented."""
    if not line.strip():
        return open_now  # a blank line does not close an indented block
    if not line.startswith(("    ", "\t")):
        return False
    return open_now or (prev_blank and not in_list)


# A backticked `` `references/<file>.md` `` inside a SKILL.md: the skill's
# reference-library pointer, written as prose (axis 2 only follows real
# markdown links). Unlike the deliberately-unsafe generic backtick path, this
# shape is unambiguous: it must resolve to a `references/` dir of the skill or
# one of its ancestors (a nested skill shares its parent's library — the
# ava_builtins/skills/ava-serious-engineering tree's ai-era/ and principles/
# skills all point at
# the root library). Three review rounds lost two of these to library moves
# (Task #939).
_SKILL_REF = re.compile(r"`references/([A-Za-z0-9._/-]+\.md)`")


_SKILL_ROOTS = (
    REPO / "ava_builtins" / "skills",
    REPO / ".agents" / "skills",
)


def _resolve_skill_reference(skill_dir: Path, name: str) -> bool:
    """True when `references/<name>` exists in `skill_dir` or any ancestor up to
    either repo skill root. The nearest match wins, so a nested skill with its own
    library shadows the parent's for that file.

    The walk also stops at the filesystem root — a doc under a test's tmp_path
    (or any checkout that is not this repo) simply resolves against its own
    tree and returns False once the root is reached; it must never loop."""
    cur = skill_dir
    while True:
        if (cur / "references" / name).exists():
            return True
        if cur in (*_SKILL_ROOTS, REPO) or cur.parent == cur:
            return False
        cur = cur.parent


def check_skill_backtick_refs(doc: Path, line: str) -> list[str]:
    """Backticked `` `references/<file>.md` `` refs in a SKILL.md that do not
    resolve. Placeholders (`<name>`, `*`) are skipped — a template, not a
    pointer."""
    missing: list[str] = []
    for match in _SKILL_REF.finditer(line):
        name = match.group(1)
        if any(ch in name for ch in "<*?"):
            continue
        if not _resolve_skill_reference(doc.parent, name):
            missing.append(f"references/{name}")
    return missing


# A `skills/<name>/...` path written WITHOUT the load-dir prefix. The repo
# has no top-level `skills/` dir since #864 moved built-ins to
# `ava_builtins/skills/` (+ project skills to `.agents/skills/`), so a bare
# `skills/<name>/...` reference can only resolve from a stale checkout — and a
# SKILL.md command written that way fails when the agent runs it (audit round 2,
# skills-plugins #2: gmail/sms/web-sources/web-ai/ava-self-evolution/ava-ui all
# shipped dead `.venv/bin/python skills/<name>/...` invocations). The load-dir
# form is `$AVA_HOME/skills/<name>/...` (or `~/.ava/skills/...`).
_SKILLS_PREFIX_REF = re.compile(r"(?<![\w$./])skills/[A-Za-z0-9_-]+/")


def check_skills_prefix_refs(line: str) -> list[str]:
    """Bare `skills/<name>/` refs on this SKILL.md line, if any.

    A line that already carries a load-dir form (`$AVA_HOME/skills/` or
    `~/.ava/skills/`) is clean: the bare form can then be a word about the path
    rather than the path itself (e.g. "under `$AVA_HOME/skills/<name>/`"
    prose)."""
    if "$AVA_HOME/skills/" in line or "~/.ava/skills/" in line:
        return []
    return [m.group(0) for m in _SKILLS_PREFIX_REF.finditer(line)]


def check_links(doc: Path, line: str, *, allow_planned: bool = False) -> list[str]:
    """Relative targets on this line that do not resolve.

    `allow_planned` is `future/`'s opt-out: a link immediately followed by
    `(planned)` names something the plan proposes, not a reference gone stale."""
    missing: list[str] = []
    for match in _MD_LINK.finditer(line):
        target = match.group(1)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        # `[Title](<slug>.md)` is a template for a line the agent will write,
        # not a link — angle brackets are this repo's placeholder syntax.
        if "<" in target or ">" in target:
            continue
        if _in_code_span(line, match.start()):
            continue
        if (doc.parent / target.split("#", 1)[0]).exists():
            continue
        if allow_planned and _PLANNED.match(line, match.end()):
            continue
        missing.append(target)
    return missing


def check_doc(
    doc: Path,
    commands: dict[tuple[str, ...], set[str]],
    *,
    skip_flags: bool = False,
    allow_planned: bool = False,
) -> list[tuple[int, str]]:
    """Every dangling reference in one doc, as `(line number, message)`.

    The two checks read the same block state in opposite directions: a command
    is only an invocation when it IS code, while a link is only a link when it
    is NOT.

    `skip_flags` / `allow_planned` are `future/`'s exemptions (a plan may
    propose a flag that does not exist yet; a link may name a file not yet
    built if marked `(planned)`) — both default off, so every other doc is
    checked in full."""
    problems: list[tuple[int, str]] = []
    carry: tuple[str, ...] | None = None
    fenced = False
    indented = False
    in_list = False
    prev_blank = True
    for lineno, line in enumerate(doc.read_text(errors="replace").splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            carry = None
            indented = False
            prev_blank = False
            continue
        indented = not fenced and _indented_code(
            line, open_now=indented, prev_blank=prev_blank, in_list=in_list
        )
        found, carry = check_flags(line, commands, carry, fenced=fenced)
        if not skip_flags:
            for command, flag in found:
                problems.append((lineno, f"`{command} {flag}` — no such flag"))
        if not (fenced or indented):
            for target in check_links(doc, line, allow_planned=allow_planned):
                problems.append((lineno, f"`{target}` — no such file"))
            # Only skills carry the backtick references/ convention — a plain
            # doc's `` `references/x.md` `` stays deliberately unchecked (it may
            # be a runtime path or a taught sample, the axis-2 rationale).
            if doc.name == "SKILL.md" and ".agents" in doc.parts:
                for target in check_skill_backtick_refs(doc, line):
                    problems.append((lineno, f"`{target}` — no such file (skill references/)"))
        # Bare `skills/<name>/...` refs are checked in fenced code too — the
        # dead `.venv/bin/python skills/...` invocations live inside code
        # blocks, where links and flags are (deliberately) not checked.
        if doc.name == "SKILL.md":
            for target in check_skills_prefix_refs(line):
                problems.append(
                    (lineno, f"`{target}` — no top-level skills/ dir (use `$AVA_HOME/skills/...`)")
                )
        if line.strip() and not line.startswith((" ", "\t")):
            in_list = bool(_LIST_ITEM.match(line))
        prev_blank = not line.strip()
    return problems


def main() -> int:
    commands = ava_flags()
    for script in (REPO / "scripts").glob("*.sh"):
        commands[(script.name,)] = shell_flags(script)

    docs = tracked_docs()
    problems: list[str] = []
    for doc in docs:
        rel = doc.relative_to(REPO)
        # decisions/ and postmortems/ — point-in-time records and never
        # rewritten, so citing the flag or file that existed THEN is the record
        # working as intended (2026-07-22-telegram-out-of-core.md names the files
        # it deleted; a postmortem names the code path as it stood during the
        # incident). Fully exempt: neither axis describes what IS true now, and a
        # CLI rename must not force history to be re-written to satisfy the linter.
        if rel.parts[0] in ("decisions", "postmortems"):
            continue
        # future/ — plans. A flag it proposes may not exist yet (skip_flags),
        # but a link it names must resolve UNLESS marked `(planned)`
        # (allow_planned) — see the module docstring / issue #1045.
        future = rel.parts[0] == "future"
        for lineno, message in check_doc(doc, commands, skip_flags=future, allow_planned=future):
            problems.append(f"{rel}:{lineno}: {message}")

    unique = sorted(set(problems))
    for problem in unique:
        print(problem)
    print(f"\n{len(unique)} dangling reference(s) across {len(docs)} docs", file=sys.stderr)
    return 1 if unique else 0


if __name__ == "__main__":
    raise SystemExit(main())
