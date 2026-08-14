"""shared.frontmatter — the minimal `---` parser shared by skills + commands."""

import pytest

from shared.frontmatter import FrontmatterError, parse_frontmatter


def test_parses_fields_and_body():
    fields, body = parse_frontmatter("---\nname: x\ndescription: hi there\n---\nthe body\n")
    assert fields == {"name": "x", "description": "hi there"}
    assert body == "the body\n"


def test_unquoted_colon_in_value_raises():
    """Unquoted values containing ``:`` are no longer supported —
    they must be quoted (standard YAML)."""
    with pytest.raises(FrontmatterError, match="invalid YAML"):
        parse_frontmatter("---\nargument-hint: e.g. foo: bar\n---\nb")


def test_double_quoted_value_is_unquoted():
    """A ``"..."`` value has its quotes removed — they're YAML syntax, not content
    (else a description renders with literal ``"`` in every consumer)."""
    fields, _ = parse_frontmatter('---\ndescription: "You MUST do it."\n---\nb')
    assert fields["description"] == "You MUST do it."


def test_single_quoted_value_is_unquoted():
    fields, _ = parse_frontmatter("---\nname: 'hello world'\n---\nb")
    assert fields["name"] == "hello world"


def test_quoted_value_may_contain_colon():
    """Quotes protect a ``: `` that a bare value couldn't carry in YAML — and it
    survives unquoting."""
    fields, _ = parse_frontmatter('---\ndescription: "a tool: when to use it"\n---\nb')
    assert fields["description"] == "a tool: when to use it"


def test_malformed_quoted_value_raises():
    """An opening quote with no proper close is a well-formedness error."""
    with pytest.raises(FrontmatterError, match="invalid YAML"):
        parse_frontmatter('---\nname: "unterminated\n---\nb')


def test_blank_frontmatter_lines_skipped():
    fields, _ = parse_frontmatter("---\nname: x\n\ndescription: y\n---\nb")
    assert fields == {"name": "x", "description": "y"}


def test_no_opener_raises():
    with pytest.raises(FrontmatterError, match="must start with"):
        parse_frontmatter("no frontmatter here")


def test_unterminated_raises():
    with pytest.raises(FrontmatterError, match="not closed"):
        parse_frontmatter("---\nname: x\n")


def test_non_dict_raises():
    """A frontmatter that does not parse to a dict raises."""
    with pytest.raises(FrontmatterError, match="must be key: value pairs"):
        parse_frontmatter("---\njust a line\n---\nb")


def test_literal_block_scalar():
    """YAML ``|`` block scalar preserves newlines."""
    fields, _ = parse_frontmatter(
        "---\n"
        "description: |\n"
        "  Multi-aspect, pre-MR code review.\n"
        "  Fans out across reviewers.\n"
        "---\n"
        "body\n"
    )
    assert fields["description"] == (
        "Multi-aspect, pre-MR code review.\nFans out across reviewers."
    )


def test_folded_block_scalar():
    """YAML ``>`` folded block scalar joins lines with spaces."""
    fields, _ = parse_frontmatter(
        "---\ndescription: >\n  This is a long\n  description.\n---\nbody\n"
    )
    assert "This is a long description." in fields["description"]


def test_boolean_values():
    """YAML booleans are converted to string ``true`` / ``false``."""
    fields, _ = parse_frontmatter("---\nname: x\nuser-invokable: true\n---\nbody\n")
    assert fields["user-invokable"] == "true"


def test_null_value_becomes_empty_string():
    fields, _ = parse_frontmatter("---\nname: x\noptional:\n---\nbody\n")
    assert fields["optional"] == ""


def test_numeric_value():
    fields, _ = parse_frontmatter("---\nversion: 3\n---\nbody\n")
    assert fields["version"] == "3"


# ─── Agent Skills standard tolerance ──────────────────────────────────────
#
# A SKILL.md is a file Ava receives from the wider ecosystem, so anything the
# published standard allows has to parse. Rejecting a valid standard skill is
# an incompatibility, not a fail-fast.


def test_crlf_line_endings():
    """A skill authored on Windows carries CRLF; the content is standard."""
    fields, body = parse_frontmatter("---\r\nname: x\r\ndescription: y\r\n---\r\n\r\nbody\r\n")
    assert fields == {"name": "x", "description": "y"}
    assert body == "\nbody\n"


def test_utf8_bom_stripped():
    fields, _ = parse_frontmatter("﻿---\nname: x\ndescription: y\n---\nbody\n")
    assert fields == {"name": "x", "description": "y"}


def test_closing_fence_at_end_of_file():
    """Frontmatter-only SKILL.md, no trailing newline after the closing fence."""
    fields, body = parse_frontmatter("---\nname: x\ndescription: y\n---")
    assert fields == {"name": "x", "description": "y"}
    assert body == ""


def test_optional_standard_fields_preserved():
    """`license` / `compatibility` / `allowed-tools` / `metadata` are optional
    fields of the Agent Skills spec. The parser returns them; each caller reads
    the keys it honors and ignores the rest."""
    fields, _ = parse_frontmatter(
        "---\n"
        "name: pdf-processing\n"
        "description: Extract PDF text. Use when handling PDFs.\n"
        "license: Apache-2.0\n"
        "compatibility: Requires Python 3.14+ and uv\n"
        "allowed-tools: Bash(git:*) Bash(jq:*) Read\n"
        "metadata:\n"
        "  author: example-org\n"
        '  version: "1.0"\n'
        "---\n"
        "body\n"
    )
    assert fields["license"] == "Apache-2.0"
    assert fields["compatibility"] == "Requires Python 3.14+ and uv"
    assert fields["allowed-tools"] == "Bash(git:*) Bash(jq:*) Read"
    assert "example-org" in fields["metadata"]  # nested mapping, stringified


def test_unknown_field_is_not_an_error():
    """A client-specific extension key parses like any other field."""
    fields, _ = parse_frontmatter("---\nname: x\ndescription: y\nx-vendor-key: v\n---\nbody\n")
    assert fields["x-vendor-key"] == "v"
