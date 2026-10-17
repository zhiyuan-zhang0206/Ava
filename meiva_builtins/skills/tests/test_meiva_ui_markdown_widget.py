from __future__ import annotations

from pathlib import Path

_TEMPLATE = (
    Path(__file__).parents[3]
    / "meiva_builtins"
    / "skills"
    / "platform"
    / "meiva-guide"
    / "pages"
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


_TSX = _TEMPLATE.parent / "Markdown.tsx"


def test_template_keeps_a_lone_tilde_literal() -> None:
    template = _TEMPLATE.read_text()
    assert "startsWith('~~')" in template, "the single-tilde guard must stay"
    assert "marked.use({" in template


def test_react_widget_disables_single_tilde() -> None:
    assert "[remarkGfm, { singleTilde: false }]" in _TSX.read_text()
