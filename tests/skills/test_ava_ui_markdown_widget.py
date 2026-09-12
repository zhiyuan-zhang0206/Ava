"""Invariant tests for the ava-ui markdown widget template.

`ava_builtins/skills/ava-ui/widgets/markdown/md.html` is a template an agent
copies into a page directory and injects Markdown into. It used to carry the
`{{MARKDOWN_CONTENT}}` token twice — once in the descriptive header comment and
once in the real `<script type="text/markdown" id="md-source">` slot — while the
widget README taught a file-wide replace, so following the README injected the
whole Markdown into the comment as well: the page still rendered, but the
artifact doubled (92,155 bytes doubled vs 48,106 bytes slot-only), and a body
containing `--` also made the comment illegal HTML (task #3185).

These tests lock the template invariant and the replacement behavior — exactly
one token, inside the md-source slot, and a slot-only replacement that injects
the Markdown once — not the rendering.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-ui"
    / "widgets"
    / "markdown"
    / "md.html"
)
_PLACEHOLDER = "{{MARKDOWN_CONTENT}}"
_SLOT = f'<script type="text/markdown" id="md-source" hidden>{_PLACEHOLDER}</script>'


def test_template_carries_the_token_only_in_the_md_source_slot() -> None:
    template = _TEMPLATE.read_text()
    assert template.count(_SLOT) == 1, "the md-source slot must exist exactly once"
    assert template.count(_PLACEHOLDER) == template.count(_SLOT), (
        "every token must live inside the md-source slot: a second one in the header "
        "comment makes a file-wide replace inject the markdown twice (task #3185)"
    )


def test_slot_only_replacement_injects_the_markdown_once() -> None:
    template = _TEMPLATE.read_text()
    markdown = "# t\n\na -- b\n"
    safe = markdown.replace("</script>", "<\\/script>")
    html = template.replace(_SLOT, _SLOT.replace(_PLACEHOLDER, safe))
    assert html.count("a -- b") == 1, "the markdown must be injected exactly once"
    assert _PLACEHOLDER not in html, "no placeholder may survive the injection"
