"""Lists of top-level packages that other files keep, generated from `[roles]`.

A file that needs the list in a form only text can hold (a TOML array, a regular
expression in YAML, a Python literal the application cannot import `meizel` for) carries
a marked region:

    # meizel:roles BEGIN names application tooling
    "agent",
    ...
    # meizel:roles END

`write` regenerates every region from the root manifest and `check` reports a region
that is stale. A file with a region is a generated output, and the only file outside
`meizel/` that may list the packages of a role.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from meizel import roles as role_api
from meizel.graph.model import MeizelError

__all__ = ["Region", "regions", "stale", "write"]

MARKER = "meizel:roles"
_BEGIN = re.compile(rf"^(?P<indent>\s*)#\s*{MARKER}\s+BEGIN\s+(?P<format>\S+)\s+(?P<roles>.+?)\s*$")
_END = re.compile(rf"^\s*#\s*{MARKER}\s+END\s*$")
_TEXT_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".sh")


@dataclass(frozen=True, slots=True)
class Region:
    path: str
    begin: int  # 1-based line of the BEGIN marker
    end: int  # 1-based line of the END marker
    indent: str
    format: str
    roles: tuple[str, ...]

    def render(self, root: Path) -> list[str]:
        names = role_api.packages(*self.roles, root=root)
        if self.format == "names":
            return [f'{self.indent}"{name}",' for name in names]
        if self.format == "testpaths":
            hosts = [
                n if n in role_api.packages("tests", root=root) else f"{n}/**/tests" for n in names
            ]
            return [f'{self.indent}"{host}",' for host in hosts]
        if self.format == "dirs":
            return [f"{self.indent}{name}/" for name in names]
        if self.format == "regex-names":
            return [f"{self.indent}{'|'.join(names)}"]
        raise MeizelError(f"{self.path}:{self.begin}: unknown generated format {self.format!r}")


def _candidates(root: Path) -> list[str]:
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "grep", "-l", "-F", MARKER, "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode not in (0, 1):
        raise MeizelError(f"git grep failed: {done.stderr.strip()}")
    return sorted(
        p
        for p in done.stdout.splitlines()
        if p.endswith(_TEXT_SUFFIXES) and not p.startswith("meizel/")
    )


def regions(root: Path) -> list[Region]:
    """Every marked region of every tracked file outside `meizel/`."""
    found: list[Region] = []
    for path in _candidates(root):
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        open_region: re.Match[str] | None = None
        start = 0
        for number, text in enumerate(lines, 1):
            begin = _BEGIN.match(text)
            if begin:
                if open_region is not None:
                    raise MeizelError(f"{path}:{number}: a region begins inside the one at {start}")
                open_region, start = begin, number
            elif _END.match(text):
                if open_region is None:
                    raise MeizelError(f"{path}:{number}: END without BEGIN")
                found.append(
                    Region(
                        path,
                        start,
                        number,
                        open_region["indent"],
                        open_region["format"],
                        tuple(open_region["roles"].split()),
                    )
                )
                open_region = None
        if open_region is not None:
            raise MeizelError(f"{path}:{start}: BEGIN without END")
    return found


def stale(root: Path) -> list[str]:
    """One message per region whose content differs from what the roles generate."""
    messages: list[str] = []
    for region in regions(root):
        lines = (root / region.path).read_text(encoding="utf-8").splitlines()
        if lines[region.begin : region.end - 1] != region.render(root):
            messages.append(
                f"{region.path}:{region.begin}: generated {region.format} region is stale; "
                "run `python -m meizel roles write`"
            )
    return messages


def write(root: Path) -> list[str]:
    """Regenerate every region; returns one line per file rewritten."""
    changed: list[str] = []
    by_path: dict[str, list[Region]] = {}
    for region in regions(root):
        by_path.setdefault(region.path, []).append(region)
    for path, items in by_path.items():
        full = root / path
        text = full.read_text(encoding="utf-8")
        lines = text.splitlines()
        for region in sorted(items, key=lambda r: r.begin, reverse=True):
            lines[region.begin : region.end - 1] = region.render(root)
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        if updated != text:
            full.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed
