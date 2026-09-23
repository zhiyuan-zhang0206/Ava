"""Minimal `---`-delimited frontmatter parser shared by skills and commands.

Frontmatter is parsed as YAML (`yaml.safe_load`).  Values that contain
a ``: `` must be quoted (e.g. ``description: "a tool: when to use it"``)
or use a YAML block scalar (``|`` / ``>``) for multi-line text.

Required-field validation is left to each caller (skills require
name+description, commands derive the name from the filename), so this module
only enforces structural well-formedness.

The input is normalized before parsing — a leading UTF-8 BOM is dropped and
CR / CRLF line endings become LF — because a SKILL.md is a file Ava *receives*
from the Agent Skills ecosystem, not one it authors: a skill written on Windows
or exported by an editor that emits a BOM is a perfectly valid standard skill,
and rejecting it would be rejecting the encoding, not the content. Every key
that is present is returned, so a skill carrying optional standard fields
(``license`` / ``compatibility`` / ``metadata`` / ``allowed-tools``) or a
client's own extensions parses unchanged; callers read the keys they honor and
ignore the rest.
"""

from __future__ import annotations

from typing import Any, cast

import yaml


class FrontmatterError(ValueError):
    """Frontmatter is structurally malformed — no `---` opener, unterminated
    block, a line that is not in `key: value` form, or a quoted value that is
    not a well-formed quoted scalar."""


_BOM = "\ufeff"


def _normalize(content: str) -> str:
    """Drop a leading UTF-8 BOM and fold CRLF / lone CR to LF, so the delimiter
    scan below works on one canonical line ending."""
    if content.startswith(_BOM):
        content = content[len(_BOM) :]
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _split_frontmatter(content: str) -> tuple[str, str] | None:
    """Split a normalized `---`-delimited frontmatter block; shared by the
    strict and typed parsers so the delimiter scan lives in exactly one place.

    Returns `(fm_text, body)` — the raw YAML block and everything after the
    closing fence — or `None` when there is no opener or the block is not
    closed. No YAML is parsed here; value coercion is the caller's contract.
    """
    content = _normalize(content)
    if not content.startswith("---\n"):
        return None
    rest = content[4:]
    end = rest.find("\n---\n")
    if end >= 0:
        return rest[:end], rest[end + 5 :]
    if rest.endswith("\n---"):  # closing fence is the final line, no body
        return rest[: -len("\n---")], ""
    return None


def parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Split `---`-delimited frontmatter from the markdown body.

    Returns `(fields, body)`. `fields` maps each key to its string value;
    `body` is everything after the closing ``---`` (empty when the closing
    fence is the last line of the file). Line endings in both are LF.

    Raises:
        FrontmatterError: missing ``---`` opener, missing closing ``---``
            fence, invalid YAML in the frontmatter block, or the block does
            not parse to a dict.
    """
    content = _normalize(content)
    if not content.startswith("---\n"):
        raise FrontmatterError("no frontmatter (must start with '---\\n')")
    split = _split_frontmatter(content)
    if split is None:
        raise FrontmatterError("frontmatter not closed (missing '\\n---\\n' terminator)")
    fm_text, body = split

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise FrontmatterError(f"invalid YAML in frontmatter: {e}") from e

    if not isinstance(parsed, dict):
        raise FrontmatterError(f"frontmatter must be key: value pairs, got {type(parsed).__name__}")

    fields: dict[str, str] = {}
    for key, value in cast("dict[str, Any]", parsed).items():
        key_str = str(key)
        if value is None:
            fields[key_str] = ""
        elif isinstance(value, bool):
            fields[key_str] = "true" if value else "false"
        elif isinstance(value, str):
            fields[key_str] = value
        else:
            fields[key_str] = str(value)

    return fields, body


def parse_frontmatter_typed(content: str) -> tuple[dict[str, Any], str] | None:
    """Lenient, type-preserving frontmatter parser for note pools (memory/OKF).

    Shares the delimiter scan with `parse_frontmatter` (same normalization, same
    fences), but keeps each value's original YAML type (`tags` stays a list) and
    returns `None` for anything structurally invalid instead of raising — a note
    pool's contract is that one bad note is skipped, not fatal.

    `None` when the content has no opener, an unterminated block, invalid YAML,
    or a block that does not parse to a dict.
    """
    split = _split_frontmatter(content)
    if split is None:
        return None
    fm_text, body = split
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, Any]", parsed), body
