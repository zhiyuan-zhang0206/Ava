#!/usr/bin/env python3
"""Generate a self-contained HTML page from a markdown file.

Serves as the CLI bridge between agent-authored .md reports and
``ava.ui.serve()`` — one call produces a styled, fleet-consistent
HTML file ready to show the user.

Usage:
    .venv/bin/python scripts/generate-ui-page.py \
        --md report.md \
        --output /tmp/ui/index.html \
        --title "My Report"

    # With reply form
    .venv/bin/python scripts/generate-ui-page.py \
        --md report.md \
        --output /tmp/ui/index.html \
        --title "Decision Needed" \
        --reply

Design:
    - Pure Python stdlib — no extra deps.
    - Generated HTML is self-contained: CSS inline, JS loaded from CDN
      (marked.js + highlight.js), markdown content embedded as JSON.
    - Visual style mirrors the Ava Fleet View design tokens (oklch colors,
      Inter font stack, shadcn radius scale).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

CSS = r"""/* === Ava Fleet View design tokens === */
:root {
  --background: oklch(1 0 0);
  --foreground: oklch(0.145 0 0);
  --card: oklch(1 0 0);
  --card-foreground: oklch(0.145 0 0);
  --primary: oklch(0.205 0 0);
  --primary-foreground: oklch(0.985 0 0);
  --secondary: oklch(0.97 0 0);
  --secondary-foreground: oklch(0.205 0 0);
  --muted: oklch(0.97 0 0);
  --muted-foreground: oklch(0.556 0 0);
  --accent: oklch(0.97 0 0);
  --accent-foreground: oklch(0.205 0 0);
  --destructive: oklch(0.577 0.245 27.325);
  --border: oklch(0.922 0 0);
  --input: oklch(0.922 0 0);
  --ring: oklch(0.708 0 0);
  --radius: 0.625rem;
  --radius-sm: calc(var(--radius) * 0.6);
  --radius-md: calc(var(--radius) * 0.8);
  --radius-lg: var(--radius);
  --radius-xl: calc(var(--radius) * 1.4);
  /* syntax highlight (VSCode Light+) */
  --syntax-keyword: #af00db;
  --syntax-string: #a31515;
  --syntax-comment: #008000;
  --syntax-number: #098658;
  --syntax-function: #795e26;
  --syntax-builtin: #267f99;
  --syntax-punct: #000000;
}

/* === Reset & base === */
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html {
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "PingFang SC",
    "Microsoft YaHei", "Noto Sans SC", "Hiragino Sans GB", sans-serif;
  background: var(--background);
  color: var(--foreground);
  line-height: 1.6;
  min-height: 100vh;
}

/* === Layout === */
.container {
  max-width: 48rem;
  margin: 0 auto;
  padding: 2rem 1.5rem 3rem;
}

/* === Header === */
.page-header {
  margin-bottom: 2rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--border);
}

.page-header h1 {
  font-size: 1.5rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--foreground);
}

.page-header .meta {
  font-size: 0.8125rem;
  color: var(--muted-foreground);
  margin-top: 0.375rem;
}

/* === Markdown content === */
.content {
  font-size: 0.9375rem;
  line-height: 1.7;
}

/* Typography */
.content h1 { font-size: 1.5rem; font-weight: 600; margin: 2rem 0 0.75rem; letter-spacing: -0.02em; }
.content h2 { font-size: 1.25rem; font-weight: 600; margin: 1.75rem 0 0.625rem; letter-spacing: -0.015em; }
.content h3 { font-size: 1.0625rem; font-weight: 600; margin: 1.5rem 0 0.5rem; }
.content h4 { font-size: 0.9375rem; font-weight: 600; margin: 1.25rem 0 0.375rem; }
.content h1:first-child { margin-top: 0; }

.content p {
  margin: 0.75rem 0;
}

.content a {
  color: var(--foreground);
  text-decoration: underline;
  text-underline-offset: 2px;
  text-decoration-color: var(--muted-foreground);
}

.content a:hover {
  text-decoration-color: var(--foreground);
}

/* Lists */
.content ul, .content ol {
  padding-left: 1.5rem;
  margin: 0.75rem 0;
}

.content li {
  margin: 0.25rem 0;
}

.content li > ul, .content li > ol {
  margin: 0.25rem 0;
}

/* Blockquotes */
.content blockquote {
  border-left: 3px solid var(--border);
  padding: 0.25rem 0 0.25rem 1rem;
  margin: 0.75rem 0;
  color: var(--muted-foreground);
}

/* Horizontal rule */
.content hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 1.5rem 0;
}

/* Inline code */
.content code:not(pre code) {
  font-family: "Geist Mono", "SF Mono", "Fira Code", "Fira Mono", Menlo, Consolas, monospace;
  font-size: 0.85em;
  background: var(--secondary);
  padding: 0.15em 0.35em;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}

/* Fenced code blocks */
.content pre {
  background: oklch(0.985 0 0);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 1rem;
  margin: 0.75rem 0;
  overflow-x: auto;
  font-size: 0.8125rem;
  line-height: 1.5;
}

.content pre code {
  font-family: "Geist Mono", "SF Mono", "Fira Code", "Fira Mono", Menlo, Consolas, monospace;
  background: none;
  padding: 0;
  border: none;
}

/* Tables (GFM) */
.content table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 0.875rem;
}

.content thead {
  border-bottom: 2px solid var(--border);
}

.content th {
  text-align: left;
  padding: 0.5rem 0.75rem;
  font-weight: 600;
  color: var(--muted-foreground);
  font-size: 0.8125rem;
}

.content td {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
}

.content tr:last-child td {
  border-bottom: none;
}

/* Images */
.content img {
  max-width: 100%;
  height: auto;
  border-radius: var(--radius-md);
  margin: 0.5rem 0;
}

/* Task lists (GFM) */
.content input[type="checkbox"] {
  margin-right: 0.375rem;
  accent-color: var(--primary);
}

/* === Reply form === */
.reply-section {
  margin-top: 2.5rem;
  padding-top: 1.5rem;
  border-top: 1px solid var(--border);
}

.reply-section h3 {
  font-size: 1rem;
  font-weight: 600;
  margin-bottom: 0.75rem;
}

.reply-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.reply-form textarea {
  width: 100%;
  min-height: 6rem;
  padding: 0.625rem 0.75rem;
  font-family: inherit;
  font-size: 0.875rem;
  line-height: 1.5;
  color: var(--foreground);
  background: var(--background);
  border: 1px solid var(--input);
  border-radius: var(--radius-md);
  resize: vertical;
  transition: border-color 0.15s ease;
}

.reply-form textarea:focus {
  outline: none;
  border-color: var(--ring);
  box-shadow: 0 0 0 3px oklch(0.708 0 0 / 25%);
}

.reply-form textarea::placeholder {
  color: var(--muted-foreground);
}

.reply-form button {
  align-self: flex-end;
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.5rem 1rem;
  font-family: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--primary-foreground);
  background: var(--primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: opacity 0.15s ease;
}

.reply-form button:hover {
  opacity: 0.9;
}

.reply-form button:active {
  opacity: 0.8;
}

.reply-status {
  margin-top: 0.5rem;
  font-size: 0.8125rem;
  min-height: 1.25rem;
}

.reply-status.success {
  color: oklch(0.45 0.18 150);
}

.reply-status.error {
  color: var(--destructive);
}

/* === Responsive === */
@media (max-width: 640px) {
  .container {
    padding: 1rem 1rem 2rem;
  }

  .content pre {
    padding: 0.75rem;
    font-size: 0.75rem;
  }
}
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="container">
  <header class="page-header">
    <h1>{title}</h1>
    <div class="meta">Generated {generated_at} &middot; Ava</div>
  </header>
  <main class="content" id="content"></main>
  {reply_html}
</div>

<script id="md-content" type="application/json">{md_json}</script>
<script src="https://cdn.jsdelivr.net/npm/marked@4/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/highlight.min.js"></script>
<script>
(function() {{
  "use strict";

  const mdEl = document.getElementById("md-content");
  const contentEl = document.getElementById("content");

  if (!mdEl || !contentEl) return;

  let mdText = "";
  try {{
    mdText = JSON.parse(mdEl.textContent || "");
  }} catch (e) {{
    contentEl.textContent = "Error: failed to parse markdown content.";
    return;
  }}

  // Configure marked (v4 API)
  if (typeof marked !== "undefined") {{
    // Use marked v4 highlight option for syntax highlighting
    const renderer = new marked.Renderer();
    // Override code renderer for highlight.js integration
    const origCode = renderer.code.bind(renderer);
    renderer.code = function(code, language) {{
      if (language && typeof hljs !== "undefined" && hljs.getLanguage(language)) {{
        try {{
          const highlighted = hljs.highlight(code, {{ language: language }}).value;
          return '<pre><code class="hljs language-' + language + '">' + highlighted + '</code></pre>';
        }} catch (e) {{ /* fall through */ }}
      }}
      return origCode.call(this, code, language);
    }};

    contentEl.innerHTML = marked.parse(mdText, {{
      gfm: true,
      breaks: false,
      renderer: renderer,
    }});
  }} else {{
    // Graceful degradation: render as plain text
    contentEl.textContent = mdText;
  }}
}})();
</script>
{reply_js}
</body>
</html>
"""

REPLY_HTML = """\
<section class="reply-section">
  <h3>Reply</h3>
  <form class="reply-form" id="reply-form" onsubmit="return handleReply(event)">
    <textarea id="reply-text" placeholder="Type your reply..." required></textarea>
    <button type="submit">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      Submit
    </button>
  </form>
  <div class="reply-status" id="reply-status"></div>
</section>
"""

REPLY_JS = """\
<script>
(function() {
  "use strict";

  const REPLY_API_PATH = "/api/reply";

  window.handleReply = function(event) {
    event.preventDefault();
    const textarea = document.getElementById("reply-text");
    const statusEl = document.getElementById("reply-status");
    const text = textarea.value.trim();

    if (!text) return false;

    statusEl.textContent = "Sending...";
    statusEl.className = "reply-status";

    fetch(REPLY_API_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reply: text }),
    })
      .then(function(res) {
        if (!res.ok) throw new Error("Server responded " + res.status);
        return res.json();
      })
      .then(function() {
        statusEl.textContent = "Reply sent.";
        statusEl.className = "reply-status success";
        textarea.value = "";
        textarea.disabled = true;
        document.querySelector(".reply-form button").disabled = true;
      })
      .catch(function(err) {
        statusEl.textContent = "Failed to send: " + err.message;
        statusEl.className = "reply-status error";
      });

    return false;
  };
})();
</script>
"""


def _escape_json(text: str) -> str:
    r"""JSON-encode the markdown text for safe embedding in a <script> tag.

    The ``<\/`` escape is critical: a literal ``</script>`` (or any ``</``)
    inside a ``<script>`` tag closes the tag in the HTML parser, even when
    inside a JSON string.  ``\/`` is a JSON-no-op escape sequence (solidus)
    that round-trips correctly through ``JSON.parse`` while preventing the
    HTML parser from seeing a closing tag.
    """
    encoded = json.dumps(text, ensure_ascii=False)
    return encoded.replace("</", "<\\/")


def generate(
    *,
    md_path: Path,
    output_path: Path,
    title: str,
    reply: bool = False,
) -> None:
    """Read markdown, generate self-contained HTML, write to output_path."""

    md_text = md_path.read_text(encoding="utf-8")
    md_json = _escape_json(md_text)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    reply_html = REPLY_HTML if reply else ""
    reply_js = REPLY_JS if reply else ""

    html = HTML_TEMPLATE.format(
        title=title,
        css=CSS,
        generated_at=generated_at,
        md_json=md_json,
        reply_html=reply_html,
        reply_js=reply_js,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Page written to {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a self-contained HTML page from markdown.",
    )
    parser.add_argument(
        "--md",
        required=True,
        type=Path,
        help="Path to the markdown input file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path for the output HTML file (parent dirs created if needed).",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Page title shown in the header and <title> tag.",
    )
    parser.add_argument(
        "--reply",
        action="store_true",
        default=False,
        help="Include a reply textarea + submit button at the bottom.",
    )

    args = parser.parse_args()

    if not args.md.is_file():
        print(f"Error: --md file not found: {args.md}", file=sys.stderr)
        return 1

    generate(
        md_path=args.md,
        output_path=args.output,
        title=args.title,
        reply=args.reply,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
