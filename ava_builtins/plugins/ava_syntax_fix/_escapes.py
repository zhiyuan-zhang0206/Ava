"""
Step 3 of the syntax-fix pipeline: invalid-escape-sequence fix.

Python 3.12 hardened SyntaxWarning for invalid escape sequences
(backslash followed by a non-escape char); 3.14 promotes it to
SyntaxError. Only the invalid pairs are double-escaped so valid
escapes stay untouched. Split out of plugin.py (2026-08-07, Task #1011).
"""

from __future__ import annotations

import io
import re
import tokenize

from ._punct import _line_starts

# ---------------------------------------------------------------------------
# 3. Invalid-escape-sequence fix
# ---------------------------------------------------------------------------
#
# Python 3.12 hardened SyntaxWarning for invalid escape sequences (backslash
# followed by a non-escape char). 3.14 promotes it to SyntaxError. Agents
# writing grep / sed regex inside non-raw strings hit this repeatedly when
# they pass alternation patterns (backslash-pipe) through subprocess.run.
#
# Python keeps the backslash as a literal byte and emits a warning, but the
# subprocess still runs (less correctly). Agents reading their own stdout
# rarely react to the warning. Fix here by double-escaping only the invalid
# pairs so valid escapes (newline, tab, etc.) are preserved untouched.
#
# Raw strings and byte strings follow the same escape rules; both are
# handled by the same scanner.

# Single-char valid escapes in a Python str / bytes literal.
# `\<newline>` is line continuation (handled below; explicit so the set
# stays Python identifiers + escape-char letters).
_VALID_SINGLE_ESCAPE_CHARS = set("\\'\"abfnrtv0\n")

# After `\`, these characters open multi-char escapes that consume more
# input: `\xHH`, `\uHHHH`, `\UHHHHHHHH`, `\N{name}`, `\0..\7` octal.
_MULTICHAR_ESCAPE_OPENERS = set("xuUN01234567")


def _is_invalid_escape(c: str) -> bool:
    """True iff `\\c` is an invalid Python escape sequence."""
    return c not in _VALID_SINGLE_ESCAPE_CHARS and c not in _MULTICHAR_ESCAPE_OPENERS


_STRING_PREFIX_RE = re.compile(
    r"([uUbBrRfF]{0,3})('''|\"\"\"|'|\")(.*)\2$",
    re.DOTALL,
)


def _split_string_token(text: str) -> tuple[str, str, str] | None:
    """Split a STRING token's text into (prefix, quote, body); return None
    if the structure looks malformed (no matching closing quote)."""
    m = _STRING_PREFIX_RE.match(text)
    if not m:
        return None
    return m.group(1).lower(), m.group(2), m.group(3)


def _scan_body_for_invalid_escapes(body: str, body_start_abs: int) -> list[tuple[int, int, str]]:
    """Scan a string-literal body for invalid `\\X` escapes; return absolute
    (start, end, replacement) edits ready for the right-to-left apply pass."""
    edits: list[tuple[int, int, str]] = []
    i = 0
    while i < len(body):
        if body[i] != "\\":
            i += 1
            continue
        if i + 1 >= len(body):  # lone trailing backslash; Python decides
            break
        nxt = body[i + 1]
        if _is_invalid_escape(nxt):
            abs_start = body_start_abs + i
            edits.append((abs_start, abs_start + 2, "\\\\" + nxt))
        i += 2
    return edits


def _fstring_prefix(start_text: str) -> str:
    """For an FSTRING_START token text (e.g. f-quote, rf-quote, f-triple-quote),
    return the lowercased prefix portion before the first quote character."""
    for idx, c in enumerate(start_text):
        if c in ('"', "'"):
            return start_text[:idx].lower()
    return start_text.lower()


def _string_literal_edits(
    tok: tokenize.TokenInfo, line_starts: list[int]
) -> list[tuple[int, int, str]]:
    """Invalid-escape edits for one STRING token; [] for raw strings or
    malformed tokens."""
    split = _split_string_token(tok.string)
    if split is None:
        return []
    prefix, quote, body = split
    if "r" in prefix:  # raw string — no escape interpretation
        return []
    body_start_abs = line_starts[tok.start[0] - 1] + tok.start[1] + len(prefix) + len(quote)
    return _scan_body_for_invalid_escapes(body, body_start_abs)


def _fstring_middle_edits(
    tok: tokenize.TokenInfo, line_starts: list[int]
) -> list[tuple[int, int, str]]:
    """Invalid-escape edits for one FSTRING_MIDDLE token body."""
    body_start_abs = line_starts[tok.start[0] - 1] + tok.start[1]
    return _scan_body_for_invalid_escapes(tok.string, body_start_abs)


def _collect_escape_edits(
    tokens: list[tokenize.TokenInfo], line_starts: list[int]
) -> list[tuple[int, int, str]]:
    """Walk the token stream, collecting invalid-escape edits across STRING and
    FSTRING_MIDDLE tokens. Tracks raw-ness across each FSTRING_START..FSTRING_END
    span so raw f-strings are skipped."""
    edits: list[tuple[int, int, str]] = []
    in_raw_fstring = False  # tracked across the FSTRING_START..FSTRING_END span
    for tok in tokens:
        if tok.type == tokenize.FSTRING_START:
            in_raw_fstring = "r" in _fstring_prefix(tok.string)
        elif tok.type == tokenize.FSTRING_END:
            in_raw_fstring = False
        elif tok.type == tokenize.FSTRING_MIDDLE:
            if not in_raw_fstring:
                edits.extend(_fstring_middle_edits(tok, line_starts))
        elif tok.type == tokenize.STRING:
            edits.extend(_string_literal_edits(tok, line_starts))
    return edits


def _fix_invalid_escapes(code: str) -> tuple[str, int]:
    """Double-escape every invalid-escape `\\X` inside non-raw string literals
    (regular, byte, and f-strings). Returns (fixed_code, num_replacements).

    Tokenize-based: walks STRING + FSTRING_MIDDLE tokens with `(row, col)`
    start positions and applies right-to-left absolute-offset edits so
    positions stay valid throughout. No `untokenize` — original whitespace /
    comments preserved verbatim.

    Returns the original code unchanged when tokenization fails (broken
    syntax) — that's compile()'s job to surface downstream.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return code, 0

    edits = _collect_escape_edits(tokens, _line_starts(code))
    if not edits:
        return code, 0

    fixed = code
    for abs_start, abs_end, new_text in sorted(edits, key=lambda e: -e[0]):
        fixed = fixed[:abs_start] + new_text + fixed[abs_end:]
    return fixed, len(edits)
