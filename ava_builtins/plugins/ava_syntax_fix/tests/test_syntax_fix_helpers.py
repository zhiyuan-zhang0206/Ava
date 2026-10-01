"""Helper unit tests + behavior-lock differential tests for the ava_syntax_fix
deterministic fixers.

The five high-complexity fixers (radon cc 16-19) were split into small pure
helpers. These fixers rewrite user code byte-by-byte and their output feeds
the release pipeline, so the input/output contract is sacred: the
differential tests embed the pre-refactor implementations verbatim (git HEAD)
as an oracle and assert the refactored fixers produce byte-identical output
and change counts across a corpus of edge cases.

Coverage:
- unit tests for every new helper
- differential (legacy vs refactored) on a curated edge-case corpus
- a small seeded fuzz sweep for the string/bracket scanners
"""

import random

import pytest

from ava_builtins.plugins.ava_syntax_fix._deterministic_fixes import (
    _STRING_PREFIX_CHARS,
    _compiles,
    _escape_interior_quotes,
    _escape_interior_triples,
    _find_triple_quotes,
    _find_unescaped_char,
    _first_compiling_closer,
    _first_compiling_escape,
    _first_compiling_line_escape,
    _is_raw_prefixed,
    _line_starts,
    _quote_is_string_boundary,
    _quote_positions,
    _scan_cross_line_opener,
    _scan_unbalanced_brackets,
    _string_advance,
    _syntax_error_lineno,
    _triple_quote_is_string_boundary,
    _unterminated_error,
    _unterminated_opener_pos,
    fix_bracket_matching,
    fix_escape_inner_quotes,
    fix_nested_triple_quote,
    fix_string_newlines,
    fix_unterminated_to_triple,
)


# ---------------------------------------------------------------------------
def _legacy_fix_string_newlines(code: str) -> tuple[str, int]:  # noqa: PLR0915
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
            j = 0
            while j < len(line):
                ch = line[j]

                if ch == "#":
                    break

                prefix_end = j
                while prefix_end < len(line) and line[prefix_end] in (
                    "f",
                    "r",
                    "b",
                    "u",
                    "F",
                    "R",
                    "B",
                    "U",
                ):
                    prefix_end += 1

                if prefix_end < len(line) and line[prefix_end] in ("'", '"'):
                    quote_char = line[prefix_end]
                    triple_delim = quote_char * 3

                    if line[prefix_end : prefix_end + 3] == triple_delim:
                        after_open = line[prefix_end + 3 :]
                        close_pos = after_open.find(triple_delim)
                        if close_pos >= 0:
                            j = prefix_end + 3 + close_pos + 3
                        else:
                            in_string_info = (triple_delim, triple_delim)
                            j = len(line)
                        continue

                    k = prefix_end + 1
                    closed = False
                    while k < len(line):
                        if line[k] == "\\":
                            k += 2
                            continue
                        if line[k] == quote_char:
                            closed = True
                            j = k + 1
                            break
                        k += 1

                    if closed:
                        continue

                    # Cross-line pattern: upgrade opener to triple-quote.
                    lines[i] = line[:prefix_end] + triple_delim + line[prefix_end + 1 :]
                    in_string_info = (quote_char, triple_delim)
                    changes += 1
                    j = len(line)
                    continue

                j += 1

        else:
            # Inside a converted string -- find and upgrade the closer.
            orig_delim, triple_delim = in_string_info

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
                lines[i] = line[:close_pos] + triple_delim + line[close_pos + 1 :]
                changes += 1
                in_string_info = None

    return "\n".join(lines), changes


def _legacy_fix_bracket_matching(code: str) -> tuple[str, int]:
    """Detect and fix unbalanced parentheses, brackets, and braces using a
    simple stack-based matching algorithm.

    When an unbalanced bracket is found, attempts a minimal fix:
    - Missing closing bracket: append at the end of the source.
    - Extra closing bracket: remove it.
    """
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {")": "(", "]": "[", "}": "{"}

    stack: list[tuple[str, int]] = []
    changes = 0

    # Scan character by character, skipping string literals heuristically.
    in_string: str | None = None
    in_triple: bool = False
    i = 0
    while i < len(code):
        ch = code[i]

        if not in_string and ch in ('"', "'"):
            if code[i : i + 3] in ('"""', "'" * 3):
                in_string = code[i : i + 3]
                in_triple = True
                i += 3
                continue
            in_string = ch
            i += 1
            continue
        if in_string:
            if in_triple:
                if code[i : i + 3] == in_string:
                    in_string = None
                    in_triple = False
                    i += 3
                    continue
            else:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_string:
                    in_string = None
                    i += 1
                    continue
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

    if stack:
        suffix = "".join(pairs[op] for op, _ in reversed(stack))
        code = code.rstrip() + suffix + "\n"
        changes += len(stack)

    return code, changes


def _legacy_fix_unterminated_to_triple(code: str) -> tuple[str, int]:
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
    try:
        compile(code, "<guard>", "exec")
        return code, 0
    except SyntaxError as e:
        msg = e.msg or ""
        lineno, offset = e.lineno, e.offset
    if "unterminated string literal" not in msg and "unterminated f-string literal" not in msg:
        return code, 0
    if not lineno or not offset:
        return code, 0

    op = _line_starts(code)[lineno - 1] + (offset - 1)
    # The offset may point at a string prefix (f / r / b / u) rather than the
    # quote itself; skip the prefix letters to land on the opening quote.
    while op < len(code) and code[op] in _STRING_PREFIX_CHARS:
        op += 1
    if op >= len(code) or code[op] not in ("'", '"'):
        return code, 0

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
                return candidate, 1
        i += 1
    return code, 0


def _legacy_fix_nested_triple_quote(code: str) -> tuple[str, int]:
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
        escaped = "\\" + delim[0] + "\\" + delim[0] + "\\" + delim[0]
        positions: list[int] = []
        i = 0
        while i < len(code) - 2:
            if code[i : i + 3] == delim:
                positions.append(i)
                i += 3
            else:
                i += 1
        if len(positions) < 3:
            continue

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
                out = code[:opener] + delim
                last = opener + 3
                for p in interior:
                    out += code[last:p] + escaped
                    last = p + 3
                out += code[last:closer] + delim + code[closer + 3 :]
                if _compiles(out):
                    return out, len(interior)
    return code, 0


def _legacy_fix_escape_inner_quotes(code: str) -> tuple[str, int]:
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
    try:
        compile(code, "<guard>", "exec")
        return code, 0
    except SyntaxError as e:
        lineno = e.lineno
    if not lineno:
        return code, 0
    lines = code.split("\n")
    if lineno > len(lines):
        return code, 0
    line = lines[lineno - 1]

    for quote in ('"', "'"):
        positions: list[int] = []
        i = 0
        while i < len(line):
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == quote:
                positions.append(i)
            i += 1
        if len(positions) < 3:
            continue
        for oi in range(len(positions)):
            opener = positions[oi]
            for ci in range(len(positions) - 1, oi, -1):
                interior = positions[oi + 1 : ci]
                if not interior:
                    continue
                if any(_quote_is_string_boundary(line, p) for p in interior):
                    continue
                new_line = line[: opener + 1]
                last = opener + 1
                for p in interior:
                    new_line += line[last:p] + "\\" + quote
                    last = p + 1
                new_line += line[last:]
                candidate = "\n".join([*lines[: lineno - 1], new_line, *lines[lineno:]])
                if _compiles(candidate):
                    return candidate, len(interior)
    return code, 0


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestFindUnescapedChar:
    def test_plain_char(self):
        assert _find_unescaped_char('a"b', '"', 0) == 1

    def test_skips_escaped(self):
        assert _find_unescaped_char('a\\"b"', '"', 0) == 4

    def test_not_found(self):
        assert _find_unescaped_char("abc", '"', 0) == -1

    def test_start_offset(self):
        assert _find_unescaped_char('"a"b"', '"', 3) == 4

    def test_trailing_backslash(self):
        # backslash as last char: skip past end, no match
        assert _find_unescaped_char("ab\\", '"', 0) == -1


class TestScanCrossLineOpener:
    def test_cross_line_single_quote(self):
        assert _scan_cross_line_opener("x = 'hello") == ("'", 4, False)

    def test_cross_line_double_quote(self):
        assert _scan_cross_line_opener('x = "hello') == ('"', 4, False)

    def test_prefix_skipped(self):
        assert _scan_cross_line_opener("x = f'hello") == ("'", 5, False)
        assert _scan_cross_line_opener("x = rb'hello") == ("'", 6, False)

    def test_comment_stops_scan(self):
        assert _scan_cross_line_opener("x = 1  # ' open") is None

    def test_closed_string_then_cross_line(self):
        # first string closes on the line, second does not
        assert _scan_cross_line_opener("x = 'a' 'b") == ("'", 8, False)

    def test_unclosed_triple_marks_stuck(self):
        assert _scan_cross_line_opener("x = '''unclosed") == ("'''", 4, True)
        assert _scan_cross_line_opener('x = """unclosed') == ('"""', 4, True)

    def test_closed_triple_skipped(self):
        assert _scan_cross_line_opener("x = '''a''' y = 'b") == ("'", 16, False)

    def test_no_opener(self):
        assert _scan_cross_line_opener("x = 1 + 2") is None

    def test_escaped_quote_not_closer(self):
        # the \" is escaped, so the string stays open across the line
        assert _scan_cross_line_opener("x = 'it\\'s") == ("'", 4, False)


class TestStringAdvance:
    def test_triple_close(self):
        assert _string_advance('"""abc"""', 0, '"""', in_triple=True) == (3, None, False)

    def test_triple_no_close(self):
        assert _string_advance('"""ab', 1, '"""', in_triple=True) == (2, '"""', True)

    def test_single_escape(self):
        assert _string_advance('"a\\"b"', 2, '"', in_triple=False) == (4, '"', False)

    def test_single_close(self):
        assert _string_advance('"ab"', 3, '"', in_triple=False) == (4, None, False)

    def test_single_plain(self):
        assert _string_advance('"ab"', 1, '"', in_triple=False) == (2, '"', False)


class TestScanUnbalancedBrackets:
    def test_balanced(self):
        assert _scan_unbalanced_brackets("(1, 2)") == ("(1, 2)", [], 0)

    def test_missing_closer(self):
        _code, stack, changes = _scan_unbalanced_brackets("(1, 2")
        assert changes == 0
        assert stack == [("(", 0)]

    def test_extra_closer_removed(self):
        code, stack, changes = _scan_unbalanced_brackets("(1, 2))")
        assert code == "(1, 2)"
        assert changes == 1
        assert stack == []

    def test_nested(self):
        _code, stack, changes = _scan_unbalanced_brackets("([{ }]")
        assert changes == 0
        assert [op for op, _ in stack] == ["("]

    def test_string_brackets_ignored(self):
        assert _scan_unbalanced_brackets('x = "(" + ")"') == ('x = "(" + ")"', [], 0)

    def test_triple_string_ignored(self):
        assert _scan_unbalanced_brackets('x = """( [ { ] )"""') == (
            'x = """( [ { ] )"""',
            [],
            0,
        )

    def test_unclosed_string_ignores_brackets(self):
        # an unterminated single-quoted string swallows the rest of the scan
        assert _scan_unbalanced_brackets("x = 'unclosed (string") == (
            "x = 'unclosed (string",
            [],
            0,
        )


class TestUnterminatedError:
    def test_valid_code(self):
        assert _unterminated_error("x = 1") is None

    def test_unterminated_string(self):
        assert _unterminated_error('x = "unterminated\nstill') == (1, 5)

    def test_unrelated_error(self):
        assert _unterminated_error("def f(:\n    pass") is None

    def test_unterminated_fstring(self):
        err = _unterminated_error('x = f"{v}\nstill')
        assert err is not None


class TestUnterminatedOpenerPos:
    def test_quote_position(self):
        assert _unterminated_opener_pos('x = "unterminated\nstill', 1, 5) == 4

    def test_prefix_skipped(self):
        assert _unterminated_opener_pos('x = f"unterminated\nstill', 1, 6) == 5


class TestFirstCompilingCloser:
    def test_finds_compiling_closer(self):
        assert _first_compiling_closer('x = "unterminated\nstill"', 4, '"') == (
            'x = """unterminated\nstill"""'
        )

    def test_no_closer(self):
        assert _first_compiling_closer('x = "unterminated\nstill', 4, '"') is None

    def test_body_quote_candidate_rejected(self):
        # first same-char quote in the body does not compile; the later one does
        fixed = _first_compiling_closer("r = run('cmd\n'body' more')", 9, "'")
        assert fixed is not None
        assert fixed.count("'''") == 2
        assert _compiles(fixed)


class TestFindTripleQuotes:
    def test_positions(self):
        assert _find_triple_quotes('a"""b"""c"""', '"""') == [1, 5, 9]

    def test_empty(self):
        assert _find_triple_quotes("abc", '"""') == []

    def test_overlapping_run(self):
        # """""" = two adjacent triples
        assert _find_triple_quotes('""""""', '"""') == [0, 3]


class TestEscapeInteriorTriples:
    def test_rebuild(self):
        assert _escape_interior_triples('"""a"""b"""c"""', 0, 10, [4], '"""') == (
            '"""a\\"\\"\\"b"""""""'
        )

    def test_multiple_interior(self):
        out = _escape_interior_triples('"""a"""b"""c"""d"""', 0, 14, [4, 8], '"""')
        assert out.count('\\"') == 6


class TestFirstCompilingEscape:
    def test_simple_docstring(self):
        src = 'src = """\ndef f():\n    """doc"""\n    return 1\n"""'
        result = _first_compiling_escape(src, '"""', _find_triple_quotes(src, '"""'))
        assert result is not None
        fixed, n = result
        assert n == 2
        assert _compiles(fixed)

    def test_raw_prefixed_opener_skipped(self):
        src = 'x = r"""raw\n"""doc"""\n"""'
        assert _first_compiling_escape(src, '"""', _find_triple_quotes(src, '"""')) is None

    def test_no_candidate(self):
        assert _first_compiling_escape("abc", '"""', []) is None


class TestSyntaxErrorLineno:
    def test_valid(self):
        assert _syntax_error_lineno("x = 1") is None

    def test_error_line(self):
        assert _syntax_error_lineno('x = "unterminated') == 1

    def test_second_line(self):
        assert _syntax_error_lineno("x = 1\ny = 'unterminated") == 2


class TestQuotePositions:
    def test_unescaped_positions(self):
        assert _quote_positions('a"b"c"d', '"') == [1, 3, 5]

    def test_escaped_skipped(self):
        assert _quote_positions('a\\"b"', '"') == [4]

    def test_single_quotes(self):
        assert _quote_positions("a'b'c", "'") == [1, 3]


class TestEscapeInteriorQuotes:
    def test_rebuild(self):
        assert _escape_interior_quotes('"a"b"c"', 0, [2, 4], '"') == '"a\\"b\\"c"'


class TestFirstCompilingLineEscape:
    def test_finds_compiling_escape(self):
        line = 'print(run("grep -n "pat" file"))'
        positions = _quote_positions(line, '"')
        result = _first_compiling_line_escape([line], 1, line, '"', positions)
        assert result is not None
        fixed, n = result
        assert n == 2
        assert _compiles(fixed)

    def test_boundary_quotes_rejected(self):
        line = 'print("a" + "b" + "c"'
        positions = _quote_positions(line, '"')
        assert _first_compiling_line_escape([line], 1, line, '"', positions) is None

    def test_no_candidate(self):
        assert _first_compiling_line_escape(["abc"], 1, "abc", '"', []) is None


# ---------------------------------------------------------------------------
# Differential behavior lock: refactored fixers vs verbatim legacy oracles
# ---------------------------------------------------------------------------

LEGACY = {
    "fix_string_newlines": _legacy_fix_string_newlines,
    "fix_bracket_matching": _legacy_fix_bracket_matching,
    "fix_unterminated_to_triple": _legacy_fix_unterminated_to_triple,
    "fix_nested_triple_quote": _legacy_fix_nested_triple_quote,
    "fix_escape_inner_quotes": _legacy_fix_escape_inner_quotes,
}

NEW = {
    "fix_string_newlines": fix_string_newlines,
    "fix_bracket_matching": fix_bracket_matching,
    "fix_unterminated_to_triple": fix_unterminated_to_triple,
    "fix_nested_triple_quote": fix_nested_triple_quote,
    "fix_escape_inner_quotes": fix_escape_inner_quotes,
}

CORPUS = [
    # valid / untouched inputs
    "",
    "x = 1",
    "print('hello')",
    'print("hello")',
    "x = 'hello\\nworld'",
    'x = "hello\\nworld"',
    "x = '''hello\nworld'''",
    'x = """hello\nworld"""',
    "s = 'a' + 'b'",
    's = "a" + "b"',
    "x = ('a', 'b')",
    "def f():\n    pass\n",
    "x = 1  # comment with ' quote",
    'x = 1  # comment with " quote',
    # cross-line strings
    "x = 'hello\nworld'",
    'x = "hello\nworld"',
    "x = f'hello\nworld'",
    'x = f"hello\nworld"',
    "x = r'hello\nworld'",
    "x = b'hello\nworld'",
    "x = u'hello\nworld'",
    "x = F'hello\nworld'",
    "x = R'hello\nworld'",
    "x = B'hello\nworld'",
    "x = U'hello\nworld'",
    "x = fr'hello\nworld'",
    "x = f'hello {name}\nworld'",
    "x = 'a' + 'b\nc'",
    "x = 'a\nb' + 'c\nd'",
    "x = 'it\\'s\nfine'",
    "x = 'a' # comment\n'y\nz'",
    'x = """open\n\'body\'\n"""',
    'x = """open\n"body"\n"""',
    "x = '''a\nb'",
    'x = """a\nb"',
    "x = '''a\nb' + 'c\nd'",
    "print('a\nb'); print('c\nd')",
    "# only comment ' open\nx = 1",
    "x = 'unterminated",
    'x = "unterminated',
    "x = 'a\nb\nc'",
    "x = '''closed'''\ny = 'z\nw'",
    "x = 'has\ttab\nand newline'",
    "'''doc\nstring'''\nx = 'a\nb'",
    "f'''triple\nf'''\ny = 'q\nr'",
    "x = ''",
    "x = ''\ny = 'a\nb'",
    "x = '\\'\\'\\'\nnot triple'",
    "x = 'a'  # 'b\nc'",
    "x = '''unclosed\n'''",
    'x = """unclosed\n"""',
    # bracket patterns
    "(1, 2",
    "[1, 2",
    "{'a': 1",
    "(1, 2)",
    "((()))",
    "([{ }])",
    "([{ )]",
    "())",
    "(()",
    "x = (1 + (2 * 3)",
    "x = [1, [2, 3]",
    "x = {'a': {'b': 1}",
    "x = 'unclosed (string",
    'x = "(" + ")"',
    'x = "(" + ")" )',
    'x = "( \' [ { ] )"',
    'x = """triple (string [with) brackets"""',
    "x = '(a)'",
    "x = 'a\\' + 'b' )",
    "x = ('a', 'b') )",
    "(]",
    "}{",
    "{[}]",
    "x = (lambda: 1)",
    "def f(a, b):\n    return (a + b\n",
    "x = f('a', (1, 2)",
    "x = (1,\n     2",
    'x = """\n( [ { \n""" )',
    "x = ('unterminated\n",
    "x = 1  # )",
    "x = 1 )",
    "x = (1, 2))",
    "x = ((1, 2)",
    "x = [x for x in (1, 2)",
    # unterminated-to-triple patterns
    'r = run("git commit -m \'fix\nbody\nmore")',
    'r = run(f"cd {d} && commit -m \'msg\nbody")',
    "r = run('git commit -m msg\nadd a 'quoted' word\nmore')",
    "x = 'hello world'",
    "def f(:\n    pass",
    'x = "unterminated\nstill',
    "x = 'unterminated\nstill",
    'r = run("cmd\narg1\narg2")',
    "r = run('cmd\narg1')",
    'x = f"prefix {v}\nmore',
    'x = "multi\nline" + y',
    'x = "a" "b\nc"',
    'x = "\\" + "unterminated\nstill',
    'x = "line1\nline2"\nprint("ok")',
    "x = 'no newline closer'",
    "x = 'a\nb' 'c\nd'",
    'x = f"fstring {x}\n{y}"',
    "y = 'a' * 3\nx = 'unterminated\nbody'",
    "x = 1 + 2",
    # nested triple quote patterns
    'src = """\ndef f():\n    """doc"""\n    return 1\n"""',
    "src = '''\nclass C:\n    '''doc'''\n    x = 1\n'''",
    'x = """hello\nworld"""',
    "x = '''hello\nworld'''",
    'a = """x"""\nb = """y"""\nc = """z"""\nd = (1\n',
    'ava.files.write(\n    "m.py",\n    """def helper() -> None:\n    """nested doc"""\n    pass"""\n)\nava.files.edit(\n    "m.py",\n    old="""x = 1""",\n    new="""x = 2""",\n)\n',
    'x = """a""" + """b""" + """c"""\n',
    'x = """has """ inner """ triples\nand more"""',
    "x = '''has ''' inner ''' triples'''",
    'x = r"""raw\n"""doc"""\n"""',
    'x = """one""" """two""" """three"""\ny = 1',
    'x = """\n"""a"""\n"""\n',
    'x = """\n"""a"""\n"""\n"""b"""',
    "x = '''\n'''a'''\n'''\n",
    'x = """a\n"""b"""\nc"""\ny = """d"""',
    # escape inner quotes patterns
    'print(run("grep -n "pat" file"))',
    'print("a" + "b" + "c"\n',
    'print("a" + "b")',
    "print(run('grep -n 'pat' file'))",
    "print('a' + 'b' + 'c'\n",
    "x = 'has \\'escaped\\' quotes'",
    'print(run("a "b" c" d" e"))',
    'print("x" + "y" + "z" + "w"\n',
    "s = 'word 'with' inner'",
    'x = "unterminated\n"pattern"',
    "x = 1 + \n'y' 'z' 'w'",
    "cmd = 'echo \"hi\"'",
    'print(run("a \\"esc\\" b "c" d"))',
    'print("a" "b" "c" "d"\n',
]


class TestDifferentialByteExact:
    """Refactored fixers must produce byte-identical (output, changes) to the
    pre-refactor implementations on every corpus input."""

    @staticmethod
    def _check(fixer, src):
        assert NEW[fixer](src) == LEGACY[fixer](src), (  # pyright: ignore[reportUnknownArgumentType]
            f"{fixer} diverged from legacy on {src!r}: "
            f"new={NEW[fixer](src)!r} legacy={LEGACY[fixer](src)!r}"  # pyright: ignore[reportUnknownArgumentType]
        )

    def test_string_newlines(self):
        for src in CORPUS:
            self._check("fix_string_newlines", src)  # pyright: ignore[reportUnknownMemberType]

    def test_bracket_matching(self):
        for src in CORPUS:
            self._check("fix_bracket_matching", src)  # pyright: ignore[reportUnknownMemberType]

    def test_unterminated_to_triple(self):
        for src in CORPUS:
            self._check("fix_unterminated_to_triple", src)  # pyright: ignore[reportUnknownMemberType]

    def test_nested_triple_quote(self):
        for src in CORPUS:
            self._check("fix_nested_triple_quote", src)  # pyright: ignore[reportUnknownMemberType]

    def test_escape_inner_quotes(self):
        for src in CORPUS:
            self._check("fix_escape_inner_quotes", src)  # pyright: ignore[reportUnknownMemberType]

    def test_known_outputs_byte_exact(self):
        # hardcoded expectations lock the concrete output shape, not just
        # legacy-vs-new equality
        assert fix_string_newlines("x = 'hello\nworld'") == ("x = '''hello\nworld'''", 2)
        assert fix_string_newlines("x = 'a' + 'b\nc'") == ("x = 'a' + '''b\nc'''", 2)
        # legacy quirk: an unclosed triple-quote opener freezes the scanner
        assert fix_string_newlines("x = '''a\nb' + 'c\nd'") == ("x = '''a\nb' + 'c\nd'", 0)
        assert fix_bracket_matching("(1, 2") == ("(1, 2)\n", 1)
        assert fix_bracket_matching("(1, 2))") == ("(1, 2)", 1)
        assert fix_bracket_matching("x = 'unclosed (string") == ("x = 'unclosed (string", 0)
        assert fix_unterminated_to_triple('r = run("git commit -m \'fix\nbody\nmore")') == (
            'r = run("""git commit -m \'fix\nbody\nmore""")',
            1,
        )
        assert fix_escape_inner_quotes('print(run("grep -n "pat" file"))') == (
            'print(run("grep -n \\"pat\\" file"))',
            2,
        )
        assert fix_nested_triple_quote("x = 1 + 2") == ("x = 1 + 2", 0)

    @pytest.mark.filterwarnings("ignore::SyntaxWarning")
    def test_seeded_fuzz_byte_exact(self):
        # deterministic pseudo-random sweep over quote/bracket/escape-heavy
        # inputs; both implementations must agree on every sample
        rng = random.Random(20260801)  # noqa: S311 -- deterministic seed is the point
        alphabet = list("'\"()[]{}+,=#fFrRbBuU \\") + list("abcxyz012._-*")
        for _ in range(1500):
            nlines = rng.randint(1, 5)
            src = "\n".join(
                "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))
                for _ in range(nlines)
            )
            for fixer, new_fn in NEW.items():
                assert new_fn(src) == LEGACY[fixer](src), f"{fixer} diverged from legacy on {src!r}"
