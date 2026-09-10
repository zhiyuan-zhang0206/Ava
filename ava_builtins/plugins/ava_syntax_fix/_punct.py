"""
Step 1 of the syntax-fix pipeline: Chinese punctuation -> ASCII.

Fixed in code positions only - never inside string / f-string
literals or comments (tokenize-guarded). Split out of plugin.py
(2026-08-07, Task #1011) so the plugin entry stays under the
800-line hard ceiling.
"""

from __future__ import annotations

import io
import tokenize

# ---------------------------------------------------------------------------
# 1. Chinese punctuation -> ASCII
# ---------------------------------------------------------------------------

_PUNCT_MAP: dict[int, int | str] = str.maketrans(
    {
        "\uff0c": ",",
        "\u3002": ".",
        "\uff1a": ":",
        "\uff1b": ";",
        "\uff08": "(",
        "\uff09": ")",
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\uff01": "!",
        "\uff1f": "?",
        "\u3010": "[",
        "\u3011": "]",
        "\uff5e": "~",
        "\uff20": "@",
        "\uff03": "#",
        "\uff05": "%",
        "\uff06": "&",
        "\uff0a": "*",
        "\uff0b": "+",
        "\uff0d": "-",
        "\uff0f": "/",
        "\uff1d": "=",
        "\uff1c": "<",
        "\uff1e": ">",
    }
)

_PUNCT_CHARS = {chr(k) for k in _PUNCT_MAP}


_PROTECTED_PUNCT_TOKENS = (
    tokenize.STRING,
    tokenize.FSTRING_START,
    tokenize.FSTRING_MIDDLE,
    tokenize.FSTRING_END,
    tokenize.COMMENT,
)

# Fullwidth/curly quotes -- the subset of _PUNCT_MAP that can act as a
# string delimiter. Fixed in a first pass so a fullwidth-delimited string turns
# into a real STRING token before the second pass touches its interior.
_FULLWIDTH_QUOTE_MAP: dict[int, int | str] = {0x201C: '"', 0x201D: '"', 0x2018: "'", 0x2019: "'"}


def _line_starts(code: str) -> list[int]:
    """Absolute char offset of every (1-indexed) line start, plus one past the
    end -- shared by the tokenize-based fixers to convert (row, col) token
    positions into absolute offsets."""
    starts = [0]
    for ln in code.split("\n"):
        starts.append(starts[-1] + len(ln) + 1)
    return starts


def _protected_token_spans(
    tokens: list[tokenize.TokenInfo], line_starts: list[int]
) -> list[tuple[int, int]]:
    """Absolute [start, end) char spans of STRING / FSTRING_* / COMMENT tokens
    -- the regions a punctuation fixer must never touch."""

    def _abs(pos: tuple[int, int]) -> int:
        return line_starts[pos[0] - 1] + pos[1]

    return sorted(
        (_abs(tok.start), _abs(tok.end)) for tok in tokens if tok.type in _PROTECTED_PUNCT_TOKENS
    )


def _advance_protected(protected: list[tuple[int, int]], p: int, idx: int) -> int:
    """Advance the protected-span pointer past every span ending at or before
    idx, returning the new pointer position."""
    while p < len(protected) and protected[p][1] <= idx:
        p += 1
    return p


def _protected_at(protected: list[tuple[int, int]], p: int, idx: int) -> bool:
    """True when idx lies inside the protected span at pointer p. p may point
    past the end of the list, in which case nothing is protected."""
    return p < len(protected) and protected[p][0] <= idx < protected[p][1]


_MAX_TOKENIZE_LINE_LENGTH = 1_000_000
"""Lines longer than this are never tokenized by the syntax fixers. The C
tokenizer that tokenize.generate_tokens drives tracks some column offsets in
``int``, so pathologically long lines are refused up front; no model-generated
line of valid Python comes close, and skipping a fixer leaves the code
unchanged (downstream compile() / LLM repair still run)."""


def _tokenize_fix_safe(code: str) -> list[tokenize.TokenInfo] | None:
    """Tokenize code for the syntax fixers, or return None when it cannot be
    tokenized safely. Shared by the punctuation and escape fixers: both degrade
    to a no-op on None (broken syntax -- leave it for compile()).

    The C tokenizer behind generate_tokens (CPython 3.12+) can crash instead
    of raising a tokenize error: an f-string replacement field that mixes '='
    (debug), ':'/'!' delimiters and invalid expressions makes it compute a
    negative string length and raise SystemError("Negative size passed to
    PyUnicode_New") (gh-149183; upstream fix gh-149445 targets 3.15+ only;
    on 3.13+ the same input surfaces as MemoryError instead). Fixing is a
    best-effort convenience, so anything that is not a normal tokenizer error
    degrades to a no-op rather than abort the agent host run.

    MemoryError is caught alongside the rest: the line-length ceiling above
    already refuses the inputs whose tokenization could genuinely exhaust
    memory, so a MemoryError from generate_tokens on this path is the
    pathological-input manifestation, not a real allocation failure -- and
    if memory is truly gone, the unfixed code proceeds to compile() where the
    same condition surfaces anyway.
    """
    if any(len(line) > _MAX_TOKENIZE_LINE_LENGTH for line in code.split("\n")):
        return None
    try:
        # utf-8 encode rejects lone surrogates before the C tokenizer sees
        # them; paired surrogates (e.g. emoji) round-trip unchanged.
        code.encode("utf-8")
        return list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (
        tokenize.TokenError,
        IndentationError,
        SyntaxError,
        SystemError,
        UnicodeError,
        MemoryError,
    ):
        return None


def _translate_outside_strings(code: str, charmap: dict[int, int | str]) -> tuple[str, int]:
    """Translate characters via charmap everywhere except inside string /
    f-string literals and comments. Returns (new_code, num_changes), or
    (code, 0) when tokenization fails (broken syntax -- leave it for compile()).
    """
    tokens = _tokenize_fix_safe(code)
    if tokens is None:
        return code, 0

    protected = _protected_token_spans(tokens, _line_starts(code))

    out = list(code)
    changes = 0
    p = 0
    for idx, ch in enumerate(code):
        p = _advance_protected(protected, p, idx)
        if _protected_at(protected, p, idx):
            continue
        repl = charmap.get(ord(ch))
        if repl is not None:
            out[idx] = chr(repl) if isinstance(repl, int) else repl
            changes += 1
    return ("".join(out), changes) if changes else (code, 0)


def _fix_chinese_punctuation(code: str) -> tuple[str, int]:
    """Replace fullwidth / Chinese punctuation with ASCII equivalents in code
    positions only -- never inside string / f-string literals or comments.

    Targets the model fat-fingering fullwidth punctuation in code (e.g.
    ``print(\uff08x\uff09)`` or fullwidth quotes used as string delimiters). Punctuation
    inside a string literal is the agent's intended text (a Chinese prompt's
    ``\uff0c\u300c\u300d``); rewriting it corrupts that text and can forge an accidental
    closing triple-quote that breaks the literal. Returns (fixed_code,
    num_changes).

    Two passes: first only the fullwidth double quotes (so a fullwidth-delimited
    string becomes a real STRING token), then the rest of the punctuation -- both
    skipping the char spans of STRING / FSTRING_* / COMMENT tokens. Returns the
    code unchanged when tokenization fails, rather than risk corrupting
    in-string text.
    """
    if not any(c in code for c in _PUNCT_CHARS):
        return code, 0
    after_quotes, n_quotes = _translate_outside_strings(code, _FULLWIDTH_QUOTE_MAP)
    fixed, n_rest = _translate_outside_strings(after_quotes, _PUNCT_MAP)
    total = n_quotes + n_rest
    return (fixed, total) if total else (code, 0)
