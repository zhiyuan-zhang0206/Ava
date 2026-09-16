"""Single-tilde guard for the generated-page template (task #3653).

Generated pages parse Markdown client-side with marked@4, whose GFM del rule
accepts a single tilde per side: two lone ~ in one sentence struck everything
between them. The template disables single-tilde strikethrough via a
walkTokens guard; these tests lock the guard and the emitted page.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "generate-ui-page.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_ui_page", _SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_template_disables_single_tilde_strikethrough() -> None:
    src = _SCRIPT.read_text()
    assert "walkTokens" in src
    assert 'startsWith("~~")' in src, "the single-tilde guard must stay"


def test_generated_page_carries_the_guard(tmp_path: Path) -> None:
    module = _load_module()
    md = tmp_path / "in.md"
    md.write_text("57 \u4e2a worktree \u5171 ~61G\u3002~/work\n\n~~x~~\n", encoding="utf-8")
    out = tmp_path / "out.html"
    module.generate(md_path=md, output_path=out, title="tilde")
    html = out.read_text(encoding="utf-8")
    assert 'startsWith("~~")' in html
