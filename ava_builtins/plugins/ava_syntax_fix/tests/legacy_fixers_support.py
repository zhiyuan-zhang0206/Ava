"""Pre-refactor oracles for the ava_syntax_fix deterministic fixers.

`test_syntax_fix_helpers` asserts the refactored fixers match these byte for
byte. They rewrite user code byte-by-byte, so the legacy behavior is frozen
here as the reference; the monolithic originals are split into `_legacy_*`
steps only to stay inside the structure budget, with the behavior unchanged.
"""

from ava_builtins.plugins.ava_syntax_fix._deterministic_fixes import (
    _STRING_PREFIX_CHARS,
    _compiles,
    _is_raw_prefixed,
    _line_starts,
    _quote_is_string_boundary,
    _triple_quote_is_string_boundary,
)

_LEGACY_SPECIFIER_CHARS = ("f", "r", "b", "u", "F", "R", "B", "U")
_LEGACY_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
_LEGACY_BRACKET_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _legacy_skip_prefix(line: str, j: int) -> int:
    prefix_end = j
    while prefix_end < len(line) and line[prefix_end] in _LEGACY_SPECIFIER_CHARS:
        prefix_end += 1
    return prefix_end


def _legacy_open_triple(
    line: str, prefix_end: int, triple_delim: str
) -> tuple[int, tuple[str, str] | None]:
    """A triple opener: resume after its same-line closer, or stay inside it."""
    after_open = line[prefix_end + 3 :]
    close_pos = after_open.find(triple_delim)
    if close_pos >= 0:
        return prefix_end + 3 + close_pos + 3, None
    return len(line), (triple_delim, triple_delim)


def _legacy_single_line_close(line: str, prefix_end: int, quote_char: str) -> int:
    """Index just past the same-line closer of a single-quoted opener, else -1."""
    k = prefix_end + 1
    while k < len(line):
        if line[k] == "\\":
            k += 2
            continue
        if line[k] == quote_char:
            return k + 1
        k += 1
    return -1


def _legacy_scan_line_for_opener(line: str) -> tuple[str, tuple[str, str] | None, int]:
    """Scan a line outside any string for an opener that does not close on it."""
    out_line = line
    in_string_info: tuple[str, str] | None = None
    changes = 0
    j = 0
    while j < len(line):
        ch = line[j]

        if ch == "#":
            break

        prefix_end = _legacy_skip_prefix(line, j)

        if prefix_end < len(line) and line[prefix_end] in ("'", '"'):
            quote_char = line[prefix_end]
            triple_delim = quote_char * 3

            if line[prefix_end : prefix_end + 3] == triple_delim:
                j, in_string_info = _legacy_open_triple(line, prefix_end, triple_delim)
                continue

            closed_at = _legacy_single_line_close(line, prefix_end, quote_char)
            if closed_at >= 0:
                j = closed_at
                continue

            # Cross-line pattern: upgrade opener to triple-quote.
            out_line = line[:prefix_end] + triple_delim + line[prefix_end + 1 :]
            in_string_info = (quote_char, triple_delim)
            changes += 1
            j = len(line)
            continue

        j += 1
    return out_line, in_string_info, changes


def _legacy_upgrade_closer(line: str, orig_delim: str, triple_delim: str) -> tuple[str, bool]:
    """Inside a converted string: upgrade the first unescaped closer on the line."""
    close_pos = -1
    k = 0
    while k < len(line):
        if line[k] == "\\":
            k += 2
            continue
        if line[k] == orig_delim:
            close_pos = k
            break
        k += 1

    if close_pos >= 0:
        return line[:close_pos] + triple_delim + line[close_pos + 1 :], True
    return line, False


def legacy_fix_string_newlines(code: str) -> tuple[str, int]:
    """Detect single/double-quoted strings that span multiple lines (literal
    newline between delimiters) and convert them to triple-quoted strings.

    Python tokenize fails on this pattern (unterminated string literal), so
    we operate at the source-text level with a line-by-line scan. Regular
    escape sequences like \\n inside a single-line string are left alone.

    Handles plain and f-prefixed strings (f'...', f"...").
    """
    lines = code.split("\n")
    changes = 0

    # Track string state: (original_delim, triple_delim) when inside a converted string.
    in_string_info: tuple[str, str] | None = None

    for i in range(len(lines)):
        line = lines[i]

        if in_string_info is None:
            # Not inside a string -- scan for an opening quote that does not
            # close on the same line.
            lines[i], in_string_info, line_changes = _legacy_scan_line_for_opener(line)
            changes += line_changes
        else:
            # Inside a converted string -- find and upgrade the closer.
            orig_delim, triple_delim = in_string_info
            lines[i], closed = _legacy_upgrade_closer(line, orig_delim, triple_delim)
            if closed:
                changes += 1
                in_string_info = None

    return "\n".join(lines), changes


def _legacy_string_state_step(
    code: str, i: int, in_string: str | None, in_triple: bool
) -> tuple[int, str | None, bool] | None:
    """Advance past a string opener/body/closer char; None when `code[i]` is plain code."""
    ch = code[i]

    if not in_string and ch in ('"', "'"):
        if code[i : i + 3] in ('"""', "'" * 3):
            return i + 3, code[i : i + 3], True
        return i + 1, ch, in_triple
    if not in_string:
        return None
    if in_triple:
        if code[i : i + 3] == in_string:
            return i + 3, None, False
    else:
        if ch == "\\":
            return i + 2, in_string, in_triple
        if ch == in_string:
            return i + 1, None, in_triple
    return i + 1, in_string, in_triple


def _legacy_bracket_step(code: str, i: int, stack: list[tuple[str, int]]) -> tuple[str, int, int]:
    """Handle a bracket char outside strings: (code, next index, changes made)."""
    ch = code[i]
    changes = 0
    if ch in _LEGACY_BRACKET_PAIRS:
        stack.append((ch, i))
    elif ch in _LEGACY_BRACKET_CLOSERS:
        if stack and _LEGACY_BRACKET_CLOSERS[ch] == stack[-1][0]:
            stack.pop()
        else:
            code = code[:i] + code[i + 1 :]
            changes += 1
            i -= 1
    return code, i + 1, changes


def legacy_fix_bracket_matching(code: str) -> tuple[str, int]:
    """Detect and fix unbalanced parentheses, brackets, and braces using a
    simple stack-based matching algorithm.

    When an unbalanced bracket is found, attempts a minimal fix:
    - Missing closing bracket: append at the end of the source.
    - Extra closing bracket: remove it.
    """
    stack: list[tuple[str, int]] = []
    changes = 0

    # Scan character by character, skipping string literals heuristically.
    in_string: str | None = None
    in_triple: bool = False
    i = 0
    while i < len(code):
        stepped = _legacy_string_state_step(code, i, in_string, in_triple)
        if stepped is not None:
            i, in_string, in_triple = stepped
            continue

        code, i, bracket_changes = _legacy_bracket_step(code, i, stack)
        changes += bracket_changes

    if stack:
        suffix = "".join(_LEGACY_BRACKET_PAIRS[op] for op, _ in reversed(stack))
        code = code.rstrip() + suffix + "\n"
        changes += len(stack)

    return code, changes


def _legacy_unterminated_opener(code: str) -> int | None:
    """Index of the opening quote of an unterminated string, or None."""
    try:
        compile(code, "<guard>", "exec")
        return None
    except SyntaxError as e:
        msg = e.msg or ""
        lineno, offset = e.lineno, e.offset
    if "unterminated string literal" not in msg and "unterminated f-string literal" not in msg:
        return None
    if not lineno or not offset:
        return None

    op = _line_starts(code)[lineno - 1] + (offset - 1)
    # The offset may point at a string prefix (f / r / b / u) rather than the
    # quote itself; skip the prefix letters to land on the opening quote.
    while op < len(code) and code[op] in _STRING_PREFIX_CHARS:
        op += 1
    if op >= len(code) or code[op] not in ("'", '"'):
        return None
    return op


def _legacy_first_compiling_triple_conversion(code: str, op: int) -> str | None:
    """The nearest later same-char quote whose triple conversion compiles."""
    quote = code[op]
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


def legacy_fix_unterminated_to_triple(code: str) -> tuple[str, int]:
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
    op = _legacy_unterminated_opener(code)
    if op is None:
        return code, 0
    candidate = _legacy_first_compiling_triple_conversion(code, op)
    if candidate is None:
        return code, 0
    return candidate, 1


def _legacy_triple_positions(code: str, delim: str) -> list[int]:
    positions: list[int] = []
    i = 0
    while i < len(code) - 2:
        if code[i : i + 3] == delim:
            positions.append(i)
            i += 3
        else:
            i += 1
    return positions


def _legacy_escape_triples(
    code: str, delim: str, opener: int, closer: int, interior: list[int]
) -> str:
    escaped = "\\" + delim[0] + "\\" + delim[0] + "\\" + delim[0]
    out = code[:opener] + delim
    last = opener + 3
    for p in interior:
        out += code[last:p] + escaped
        last = p + 3
    out += code[last:closer] + delim + code[closer + 3 :]
    return out


def _legacy_nested_triple_for_delim(code: str, delim: str) -> tuple[str, int] | None:
    """First opener/closer pair of `delim` whose interior escape compiles."""
    positions = _legacy_triple_positions(code, delim)
    if len(positions) < 3:
        return None

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
            out = _legacy_escape_triples(code, delim, opener, closer, interior)
            if _compiles(out):
                return out, len(interior)
    return None


def legacy_fix_nested_triple_quote(code: str) -> tuple[str, int]:
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
        result = _legacy_nested_triple_for_delim(code, delim)
        if result is not None:
            return result
    return code, 0


def _legacy_error_line(code: str) -> tuple[list[str], int] | None:
    """The source lines and the 1-based SyntaxError line, when it indexes a line."""
    try:
        compile(code, "<guard>", "exec")
        return None
    except SyntaxError as e:
        lineno = e.lineno
    if not lineno:
        return None
    lines = code.split("\n")
    if lineno > len(lines):
        return None
    return lines, lineno


def _legacy_line_quote_positions(line: str, quote: str) -> list[int]:
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


def _legacy_escape_line_quotes(line: str, opener: int, interior: list[int], quote: str) -> str:
    new_line = line[: opener + 1]
    last = opener + 1
    for p in interior:
        new_line += line[last:p] + "\\" + quote
        last = p + 1
    new_line += line[last:]
    return new_line


def _legacy_first_compiling_line_escape(
    lines: list[str], lineno: int, line: str, quote: str, positions: list[int]
) -> tuple[str, int] | None:
    """First opener/closer pair of `quote` whose interior escape compiles."""
    for oi in range(len(positions)):
        opener = positions[oi]
        for ci in range(len(positions) - 1, oi, -1):
            interior = positions[oi + 1 : ci]
            if not interior:
                continue
            if any(_quote_is_string_boundary(line, p) for p in interior):
                continue
            new_line = _legacy_escape_line_quotes(line, opener, interior, quote)
            candidate = "\n".join([*lines[: lineno - 1], new_line, *lines[lineno:]])
            if _compiles(candidate):
                return candidate, len(interior)
    return None


def legacy_fix_escape_inner_quotes(code: str) -> tuple[str, int]:
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
    located = _legacy_error_line(code)
    if located is None:
        return code, 0
    lines, lineno = located
    line = lines[lineno - 1]

    for quote in ('"', "'"):
        positions = _legacy_line_quote_positions(line, quote)
        if len(positions) < 3:
            continue
        result = _legacy_first_compiling_line_escape(lines, lineno, line, quote, positions)
        if result is not None:
            return result
    return code, 0
