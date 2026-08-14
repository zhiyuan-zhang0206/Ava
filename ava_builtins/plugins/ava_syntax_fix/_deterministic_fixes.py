"""Deterministic Python syntax fixes -- patterns the LLM need not repair.

All functions follow the same contract:
    (code: str) -> tuple[str, int | None]
        Returns (fixed_code, num_changes) or (code, 0) when no fix applied.

These are new deterministic steps inserted between the existing ruff step
and compile() in the syntax_fix pipeline. Each covers a failure pattern
observed in production agent logs (see python-escape-analysis.md for the
full dataset).
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ---------------------------------------------------------------------------
# Unicode punctuation map extension
# ---------------------------------------------------------------------------

# Characters found in agent code that ruff/tokenize do not handle.
# Maps Unicode codepoint -> replacement ASCII string.
_UNICODE_PUNCT = {
    ord("\u2014"): "-",  # em-dash -> hyphen
    ord("\u2013"): "-",  # en-dash -> hyphen
    ord("\u2192"): "->",  # rightwards arrow -> ->
    ord("\u2190"): "<-",  # leftwards arrow
    ord("\u2019"): "'",  # right single quote -> apostrophe
    ord("\u2018"): "'",  # left single quote -> apostrophe
}

_UNICODE_PUNCT_CHARS = {chr(k) for k in _UNICODE_PUNCT}


def fix_unicode_punctuation(code: str) -> tuple[str, int]:
    """Replace Unicode punctuation characters with ASCII equivalents everywhere
    in the source.

    These characters never have a valid meaning in Python source. Unlike
    Chinese punctuation (which may legitimately appear inside string literals
    as user-visible text), em-dash / arrow / right-single-quote in agent code
    are always fat-fingers and can be replaced unconditionally.
    """
    if not any(c in code for c in _UNICODE_PUNCT_CHARS):
        return code, 0

    out = list(code)
    changes = 0
    for idx, ch in enumerate(code):
        repl = _UNICODE_PUNCT.get(ord(ch))
        if repl is not None:
            out[idx] = repl
            changes += 1
    return "".join(out), changes


# ---------------------------------------------------------------------------
# String delimiter fixes: cross-line strings -> triple-quoted
# ---------------------------------------------------------------------------

_STRING_NEWLINE_PATTERN = re.compile(r"\n")


def _count_leading(line: str, char: str) -> int:
    """Count consecutive `char` at the start of a line."""
    n = 0
    for c in line:
        if c == char:
            n += 1
        else:
            break
    return n


def _to_triple_quote(quote_char: str) -> str:
    """Map single quote to triple-single, double quote to triple-double."""
    return quote_char * 3


def _find_unescaped_char(line: str, char: str, start: int) -> int:
    """Index of the first unescaped `char` at/after `start` in `line`, or -1."""
    k = start
    while k < len(line):
        if line[k] == "\\":
            k += 2
            continue
        if line[k] == char:
            return k
        k += 1
    return -1


def _scan_cross_line_opener(line: str) -> tuple[str, int, bool] | None:
    """Scan `line` for a quote that opens a string not closed on this line.

    Returns (quote_char, opener_pos, stuck):
    - stuck=True: an unclosed triple-quote opener came first -- the legacy
      scanner enters a string state that never closes, so the rest of the
      source must be left untouched.
    - stuck=False: `quote_char` at `opener_pos` is a cross-line string opener.
    - None: nothing to upgrade (comment reached, or every string closes).
    """
    j = 0
    while j < len(line):
        if line[j] == "#":
            return None
        prefix_end = j
        while prefix_end < len(line) and line[prefix_end] in _STRING_PREFIX_CHARS:
            prefix_end += 1
        if prefix_end < len(line) and line[prefix_end] in ("'", '"'):
            quote_char = line[prefix_end]
            triple = quote_char * 3
            if line[prefix_end : prefix_end + 3] == triple:
                after_open = line[prefix_end + 3 :]
                close_pos = after_open.find(triple)
                if close_pos >= 0:
                    j = prefix_end + 3 + close_pos + 3
                    continue
                return (triple, prefix_end, True)
            close_pos = _find_unescaped_char(line, quote_char, prefix_end + 1)
            if close_pos >= 0:
                j = close_pos + 1
                continue
            return (quote_char, prefix_end, False)
        j += 1
    return None


def fix_string_newlines(code: str) -> tuple[str, int]:
    """Detect single/double-quoted strings that span multiple lines (literal
    newline between delimiters) and convert them to triple-quoted strings.

    Python tokenize fails on this pattern (unterminated string literal), so
    we operate at the source-text level with a line-by-line scan. Regular
    escape sequences like \\n inside a single-line string are left alone.

    Handles plain and f-prefixed strings (f'...', f"...").
    """
    lines = code.split("\n")
    changes = 0
    in_string_info: tuple[str, str] | None = None

    for i in range(len(lines)):
        line = lines[i]
        if in_string_info is None:
            found = _scan_cross_line_opener(line)
            if found is None:
                continue
            quote_char, opener_pos, stuck = found
            if stuck:
                break
            triple_delim = _to_triple_quote(quote_char)
            lines[i] = line[:opener_pos] + triple_delim + line[opener_pos + 1 :]
            in_string_info = (quote_char, triple_delim)
            changes += 1
            continue
        orig_delim, triple_delim = in_string_info
        close_pos = _find_unescaped_char(line, orig_delim, 0)
        if close_pos >= 0:
            lines[i] = line[:close_pos] + triple_delim + line[close_pos + 1 :]
            changes += 1
            in_string_info = None

    return "\n".join(lines), changes


def fix_unclosed_triple_quote(code: str) -> tuple[str, int]:
    """Detect Python source with an unclosed triple-quoted string and append
    a closing delimiter on the last non-empty line.

    This is a simple line-scan heuristic: count triple-quote openers and
    closers. If there are more openers than closers, append the missing
    closing delimiter.
    """
    if '"""' not in code and "'" * 3 not in code:
        return code, 0

    triple_double_open = code.count('"""')
    triple_single_open = code.count("'" * 3)

    if triple_double_open % 2 == 0 and triple_single_open % 2 == 0:
        return code, 0

    delimiter = '"""' if triple_double_open % 2 == 1 else "'" * 3

    stripped = code.rstrip()
    if stripped.endswith(delimiter):
        return code, 0

    fixed = stripped + delimiter + "\n"
    return fixed, 1


# ---------------------------------------------------------------------------
# Indentation normalization
# ---------------------------------------------------------------------------


def fix_indentation(code: str) -> tuple[str, int]:
    """Normalize mixed indentation by converting tabs to 4-space indents.

    Uses a leading-whitespace heuristic: only tabs before the first
    non-whitespace character on a line are converted.

    NOTE: Dataset analysis indicates the actual hit rate is low — the root
    cause for this category is mostly triple_quote_nesting, not the pattern
    this fixer targets. The compile-guard ensures no incorrect fixes are
    applied; kept as a fallback.
    """
    if "\t" not in code:
        return code, 0

    lines = code.split("\n")
    fixed_lines = []
    changes = 0
    for line in lines:
        if "\t" in line:
            stripped = line.lstrip("\t ")
            leading = line[: len(line) - len(stripped)]
            new_leading = leading.replace("\t", "    ")
            if new_leading != leading:
                changes += 1
            fixed_lines.append(new_leading + stripped)
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines), changes


# ---------------------------------------------------------------------------
# Backslash trailing cleanup
# ---------------------------------------------------------------------------

_BACKSLASH_TRAILING_RE = re.compile(r"\\\s*(\S+)\s*$")


def fix_backslash_trailing(code: str) -> tuple[str, int]:
    """Remove trailing non-whitespace characters after a line-continuation
    backslash. Python requires nothing after backslash except whitespace
    and a newline.

    NOTE: Dataset analysis indicates the actual hit rate is low — the root
    cause for this category is mostly triple_quote_nesting, not the pattern
    this fixer targets. The compile-guard ensures no incorrect fixes are
    applied; kept as a fallback.
    """
    lines = code.split("\n")
    fixed_lines = []
    changes = 0
    for line in lines:
        stripped = line.rstrip()
        bs_pos = stripped.rfind("\\")
        if bs_pos >= 0 and bs_pos < len(stripped) - 1:
            # Check that the backslash is a continuation (not part of a string)
            # and that there are non-whitespace chars after it.
            after = stripped[bs_pos + 1 :]
            if after.strip() and not stripped.endswith("\\"):
                # Keep only backslash
                fixed = stripped[: bs_pos + 1]
                fixed_lines.append(fixed)
                changes += 1
                continue
        fixed_lines.append(line)
    return "\n".join(fixed_lines), changes


# ---------------------------------------------------------------------------
# Missing comma detection
# ---------------------------------------------------------------------------

_END_EXPR_RE = re.compile(r".*[)\]}" + "'" + r'"]\s*$')
_START_EXPR_RE = re.compile(r"\s*[(\[{" '"]')


def fix_missing_comma(code: str) -> tuple[str, int]:
    """Detect adjacent multi-line expressions that are missing a separating
    comma. Common pattern: two list/dict/tuple items on consecutive lines
    without a comma between them.

    NOTE: Dataset analysis indicates the actual hit rate is low — the root
    cause for this category is mostly triple_quote_nesting, not the pattern
    this fixer targets. The compile-guard ensures no incorrect fixes are
    applied; kept as a fallback.
    """
    lines = code.split("\n")
    changes = 0
    fixed_lines: list[str] = []

    for i, line in enumerate(lines):
        fixed_lines.append(line)
        if i + 1 >= len(lines):
            continue
        this_stripped = line.rstrip()
        next_stripped = lines[i + 1].strip()

        if not this_stripped or this_stripped.startswith("#"):
            continue
        if not next_stripped or next_stripped.startswith("#"):
            continue
        if this_stripped.endswith("\\"):
            continue
        if this_stripped.endswith((",", ":", "+", "-", "*", "/", "=", "(", "[", "{")):
            continue

        if (
            _END_EXPR_RE.match(this_stripped)
            and _START_EXPR_RE.match(next_stripped)
            and not this_stripped.endswith(".")
        ):
            fixed_lines[-1] = this_stripped + ","
            changes += 1

    return "\n".join(fixed_lines), changes


# ---------------------------------------------------------------------------
# Bracket matching fix
# ---------------------------------------------------------------------------


def _string_advance(
    code: str, i: int, in_string: str, *, in_triple: bool
) -> tuple[int, str | None, bool]:
    """Advance one char inside a string literal.

    Returns (next_i, in_string, in_triple). Closes the literal when its
    delimiter is hit (escape sequences skipped in single-char strings).
    """
    if in_triple:
        if code[i : i + 3] == in_string:
            return i + 3, None, False
        return i + 1, in_string, True
    if code[i] == "\\":
        return i + 2, in_string, False
    if code[i] == in_string:
        return i + 1, None, False
    return i + 1, in_string, False


def _scan_unbalanced_brackets(code: str) -> tuple[str, list[tuple[str, int]], int]:
    """Scan `code` outside string literals, dropping extra closers in place.

    Returns (code, stack, changes): `code` with extra closing brackets
    removed, the still-open opener stack as (char, position) pairs, and the
    number of removals. Stack positions go stale as closers are removed, but
    only the chars are used downstream.
    """
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    changes = 0
    in_string: str | None = None
    in_triple = False
    i = 0
    while i < len(code):
        ch = code[i]
        if in_string:
            i, in_string, in_triple = _string_advance(code, i, in_string, in_triple=in_triple)
            continue
        if ch in ('"', "'"):
            if code[i : i + 3] in ('"""', "'''"):
                in_string, in_triple = code[i : i + 3], True
                i += 3
                continue
            in_string = ch
            i += 1
            continue
        if ch in pairs:
            stack.append((ch, i))
        elif ch in closers:
            if stack and closers[ch] == stack[-1][0]:
                stack.pop()
            else:
                code = code[:i] + code[i + 1 :]
                changes += 1
                i -= 1
        i += 1
    return code, stack, changes


def fix_bracket_matching(code: str) -> tuple[str, int]:
    """Detect and fix unbalanced parentheses, brackets, and braces using a
    simple stack-based matching algorithm.

    When an unbalanced bracket is found, attempts a minimal fix:
    - Missing closing bracket: append at the end of the source.
    - Extra closing bracket: remove it.
    """
    code, stack, changes = _scan_unbalanced_brackets(code)
    if not stack:
        return code, changes
    pairs = {"(": ")", "[": "]", "{": "}"}
    suffix = "".join(pairs[op] for op, _ in reversed(stack))
    return code.rstrip() + suffix + "\n", changes + len(stack)


# ---------------------------------------------------------------------------
# f-string empty/illegal expression fix
# ---------------------------------------------------------------------------

_FSTRING_EMPTY_RE = re.compile(r"\{\s*\}")


def fix_fstring_expressions(code: str) -> tuple[str, int]:
    """Fix common f-string expression errors.

    - Empty braces: replace with {None}.
    """
    if "f" not in code and "F" not in code:
        return code, 0

    fixed, n_empty = _FSTRING_EMPTY_RE.subn("{None}", code)
    return fixed, n_empty


# ---------------------------------------------------------------------------
# Compile guard
# ---------------------------------------------------------------------------


def _compiles(code: str) -> bool:
    """True when `code` is syntactically valid Python (compiles to a code object)."""
    try:
        compile(code, "<guard>", "exec")
    except SyntaxError:
        return False
    return True


def _line_starts(code: str) -> list[int]:
    """Absolute char offset of the start of each (1-indexed) source line."""
    starts = [0]
    for ln in code.split("\n"):
        starts.append(starts[-1] + len(ln) + 1)
    return starts


# ---------------------------------------------------------------------------
# Unterminated single/double-quoted string -> triple-quote (offset-driven)
# ---------------------------------------------------------------------------

_STRING_PREFIX_CHARS = "fFrRbBuU"


def _unterminated_error(code: str) -> tuple[int, int] | None:
    """Return (lineno, offset) of an unterminated-string SyntaxError, or None."""
    try:
        compile(code, "<guard>", "exec")
    except SyntaxError as e:
        msg = e.msg or ""
        if (
            ("unterminated string literal" in msg or "unterminated f-string literal" in msg)
            and e.lineno
            and e.offset
        ):
            return e.lineno, e.offset
    return None


def _unterminated_opener_pos(code: str, lineno: int, offset: int) -> int:
    """Char offset of the opening quote the SyntaxError points at; any string
    prefix letters (f / r / b / u) before it are skipped."""
    op = _line_starts(code)[lineno - 1] + (offset - 1)
    while op < len(code) and code[op] in _STRING_PREFIX_CHARS:
        op += 1
    return op


def _first_compiling_closer(code: str, op: int, quote: str) -> str | None:
    """Find the first later same-char quote whose triple conversion makes the
    whole source compile; return the fixed code, or None."""
    triple = quote * 3
    i = op + 1
    while i < len(code):
        ch = code[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote and "\n" in code[op:i]:
            candidate = code[:op] + triple + code[op + 1 : i] + triple + code[i + 1 :]
            if _compiles(candidate):
                return candidate
        i += 1
    return None


def fix_unterminated_to_triple(code: str) -> tuple[str, int]:
    """Convert an unterminated single/double-quoted (or f-) string into a
    triple-quoted one.

    The dominant production case is a multi-line shell command written as a
    one-line string: ``ava.shell.run("git commit -m 'msg line 1`` where the
    content (a heredoc, a commit body, a PR description) flows onto the next
    physical lines. Python reports an unterminated string because a plain
    string cannot span newlines.

    The SyntaxError offset pins the opening quote exactly (more reliable than a
    forward line scan, which the existing fix_string_newlines uses and which
    mis-fires when the body contains the same quote char). The matching closer
    is the author's intended closing quote, somewhere on a later line; it is
    found by trying each subsequent same-char quote and keeping the first whose
    conversion (opener + that quote both upgraded to triple) makes the whole
    source compile. Preferring the nearest compiling closer keeps the string as
    small as the parse allows.
    """
    err = _unterminated_error(code)
    if err is None:
        return code, 0
    lineno, offset = err
    op = _unterminated_opener_pos(code, lineno, offset)
    if op >= len(code) or code[op] not in ("'", '"'):
        return code, 0
    candidate = _first_compiling_closer(code, op, code[op])
    if candidate is None:
        return code, 0
    return candidate, 1


# ---------------------------------------------------------------------------
# Nested triple-quote: code/markdown written as a triple-quoted string whose
# body itself contains triple-quotes
# ---------------------------------------------------------------------------


def _is_raw_prefixed(code: str, pos: int) -> bool:
    """True when the string starting at `pos` carries a raw (r) prefix."""
    j = pos - 1
    prefix = ""
    while j >= 0 and code[j].isalpha():
        prefix = code[j] + prefix
        j -= 1
    return "r" in prefix.lower()


def _triple_quote_is_string_boundary(code: str, pos: int) -> bool:
    """True when the triple-quote at `pos` abuts a concatenation / collection /
    call-argument operator, so it is a real string delimiter (the opener or
    closer of a separate literal) rather than interior content.

    The triple-quote analogue of _quote_is_string_boundary, using the same
    operator set. A triple-quote hugged by ``+ , ( [ {`` on the left or
    ``+ , ) ] }`` on the right delimits its own literal -- escaping it would
    merge two adjacent literals (e.g. the ``old=`` and ``new=`` arguments of a
    single call) into one wrong-but-compiling string.
    """
    left = code[:pos].rstrip()
    right = code[pos + 3 :].lstrip()
    return bool(left and left[-1] in "+,([{") or bool(right and right[0] in "+,)]}")


def _find_triple_quotes(code: str, delim: str) -> list[int]:
    """All positions where the 3-char `delim` occurs in `code`."""
    positions: list[int] = []
    i = 0
    while i < len(code) - 2:
        if code[i : i + 3] == delim:
            positions.append(i)
            i += 3
        else:
            i += 1
    return positions


def _escape_interior_triples(
    code: str, opener: int, closer: int, interior: list[int], delim: str
) -> str:
    """Rebuild `code` escaping every triple-quote strictly between `opener`
    and `closer`; opener and closer themselves stay as `delim`."""
    escaped = "\\" + delim[0] + "\\" + delim[0] + "\\" + delim[0]
    out = code[:opener] + delim
    last = opener + 3
    for p in interior:
        out += code[last:p] + escaped
        last = p + 3
    out += code[last:closer] + delim + code[closer + 3 :]
    return out


def _first_compiling_escape(code: str, delim: str, positions: list[int]) -> tuple[str, int] | None:
    """Try opener/closer pairs in preference order; return the first escaped
    result that compiles as (fixed_code, n_escaped), or None."""
    for oi in range(len(positions)):
        if _is_raw_prefixed(code, positions[oi]):
            continue
        for ci in range(len(positions) - 1, oi, -1):
            opener, closer = positions[oi], positions[ci]
            interior = [p for p in positions if opener < p < closer]
            if not interior:
                continue
            if any(_triple_quote_is_string_boundary(code, p) for p in interior):
                continue
            out = _escape_interior_triples(code, opener, closer, interior, delim)
            if _compiles(out):
                return out, len(interior)
    return None


def fix_nested_triple_quote(code: str) -> tuple[str, int]:
    """Escape interior triple-quotes inside a triple-quoted string.

    Agents frequently build a file/string whose body is itself Python or
    markdown containing ``\"\"\"`` docstrings, e.g.
    ``ava.files.write(path, \"\"\"...def f(): \"\"\"doc\"\"\"...\"\"\")``. The
    first inner ``\"\"\"`` closes the outer string, so the rest of the body is
    parsed as code and the file fails with an assortment of downstream errors
    (invalid character, unexpected indent, unterminated string).

    The fix preserves the author's intent: the real opener and closer stay
    triple-quoted and every triple-quote strictly between them is escaped
    (``\"\"\"`` -> ``\\"\\"\\"``), which leaves the runtime string content
    byte-for-byte identical. Opener/closer are chosen by trying candidate pairs
    and keeping the first whose escaped result compiles. Raw-prefixed openers
    are skipped because a raw string cannot escape its own delimiter.

    A candidate pair is rejected when any triple-quote it would escape sits at a
    string-boundary position (hugging ``+ , ( [ {`` / ``+ , ) ] }``). Such a
    triple-quote is a real delimiter of a separate adjacent literal -- e.g. the
    closer of ``files.edit(old=\"\"\"...\"\"\", new=\"\"\"...\"\"\")``'s first
    argument -- and escaping it would silently merge two arguments into one
    string that compiles but means something else. Those cases are left for the
    repair step, where intent can be inferred, rather than corrupted here.
    """
    if _compiles(code):
        return code, 0
    for delim in ('"""', "'''"):
        positions = _find_triple_quotes(code, delim)
        if len(positions) < 3:
            continue
        result = _first_compiling_escape(code, delim, positions)
        if result is not None:
            return result
    return code, 0


# ---------------------------------------------------------------------------
# Nested same-char quote on one line: "grep -n "pattern" file" -> escape inner
# ---------------------------------------------------------------------------


def _quote_is_string_boundary(line: str, pos: int) -> bool:
    """True when the quote at `pos` abuts a concatenation / collection operator
    (so it is a real adjacent string delimiter, not a content quote)."""
    left = line[:pos].rstrip()
    right = line[pos + 1 :].lstrip()
    return bool(left and left[-1] in "+,([{") or bool(right and right[0] in "+,)]}")


def _syntax_error_lineno(code: str) -> int | None:
    """Line number of the first SyntaxError, or None when `code` compiles."""
    try:
        compile(code, "<guard>", "exec")
    except SyntaxError as e:
        return e.lineno
    return None


def _quote_positions(line: str, quote: str) -> list[int]:
    """Positions of unescaped `quote` chars in `line`."""
    positions: list[int] = []
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == quote:
            positions.append(i)
        i += 1
    return positions


def _escape_interior_quotes(line: str, opener: int, interior: list[int], quote: str) -> str:
    """Rebuild `line` with every quote in `interior` escaped (\\<quote>)."""
    new_line = line[: opener + 1]
    last = opener + 1
    for p in interior:
        new_line += line[last:p] + "\\" + quote
        last = p + 1
    new_line += line[last:]
    return new_line


def _first_compiling_line_escape(
    lines: list[str], lineno: int, line: str, quote: str, positions: list[int]
) -> tuple[str, int] | None:
    """Try opener/closer pairs on `line`; return the first candidate that makes
    the whole source compile as (fixed_code, n_escaped), or None."""
    for oi in range(len(positions)):
        opener = positions[oi]
        for ci in range(len(positions) - 1, oi, -1):
            interior = positions[oi + 1 : ci]
            if not interior:
                continue
            if any(_quote_is_string_boundary(line, p) for p in interior):
                continue
            new_line = _escape_interior_quotes(line, opener, interior, quote)
            candidate = "\n".join([*lines[: lineno - 1], new_line, *lines[lineno:]])
            if _compiles(candidate):
                return candidate, len(interior)
    return None


def fix_escape_inner_quotes(code: str) -> tuple[str, int]:
    """Escape unescaped same-char quotes nested inside a single-line string.

    Pattern: ``ava.shell.run("grep -n "pattern" file")`` -- the inner ``"``
    closes the literal early, so the shell argument is parsed as code. The fix
    escapes the interior quotes (``"pattern"`` -> ``\\"pattern\\"``).

    Restricted to interior quotes that hug content. An interior quote adjacent
    to ``+ , ( [`` is a genuine string boundary (concatenation, list, call
    argument) -- escaping it would silently merge separate literals into one
    wrong-but-compiling string, so such candidates are rejected and the error
    is left for the repair step instead.
    """
    lineno = _syntax_error_lineno(code)
    if not lineno:
        return code, 0
    lines = code.split("\n")
    if lineno > len(lines):
        return code, 0
    line = lines[lineno - 1]
    for quote in ('"', "'"):
        positions = _quote_positions(line, quote)
        if len(positions) < 3:
            continue
        result = _first_compiling_line_escape(lines, lineno, line, quote, positions)
        if result is not None:
            return result
    return code, 0


# ---------------------------------------------------------------------------
# Combined deterministic fix pass
# ---------------------------------------------------------------------------
#
# This is the single fixer registry -- no standalone json/yaml files are
# maintained separately. All additions/removals happen here only.
#
# Ordered narrow-and-safe first, then the powerful string-delimiter repairs,
# then the blunt structural fixers last. apply_all_deterministic_fixes adopts
# the first fixer whose output compiles, so earlier entries win ties -- keep the
# most precise / least content-altering fixers ahead of the blunt ones.
_FIX_FUNCTIONS: list[tuple[str, Callable[[str], tuple[str, int]]]] = [
    ("unicode_punct", fix_unicode_punctuation),
    ("fstring_expr", fix_fstring_expressions),
    ("indentation", fix_indentation),
    ("unterminated_to_triple", fix_unterminated_to_triple),
    ("nested_triple_quote", fix_nested_triple_quote),
    ("escape_inner_quotes", fix_escape_inner_quotes),
    ("string_newlines", fix_string_newlines),
    ("unclosed_triple_quote", fix_unclosed_triple_quote),
    ("missing_comma", fix_missing_comma),
    ("backslash_trailing", fix_backslash_trailing),
    ("bracket_matching", fix_bracket_matching),
]


def apply_all_deterministic_fixes(code: str) -> tuple[str, list[str]]:
    """Run the deterministic fixes, each individually compile-guarded.

    Returns (fixed_code, list_of_fix_labels_applied). Each label includes the
    change count, e.g. "unterminated_to_triple(1)".

    Compile-guarded both at the boundary and per fixer:

    - Guard 1: code that already compiles is returned untouched. The fixers
      target broken source only; running them on valid code can only corrupt
      it.
    - Per-fixer guard: each fixer is tried against the original broken source
      and its output is adopted only if it compiles, at which point the result
      is returned immediately. A fixer whose output still does not compile is
      discarded (rolled back). This prevents one fixer's correct repair from
      being clobbered by a later, blunter fixer -- the failure mode that made a
      single whole-pass guard roll everything back to broken.

    The net invariant: this never emits code that compiles worse than its input,
    and it never modifies code that already compiles.
    """
    if _compiles(code):
        return code, []

    for name, func in _FIX_FUNCTIONS:
        fixed, n = func(code)
        if n and n > 0 and _compiles(fixed):
            return fixed, [f"{name}({n})"]

    return code, []
