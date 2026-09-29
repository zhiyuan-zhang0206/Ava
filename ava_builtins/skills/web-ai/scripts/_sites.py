"""
Per-site profiles for the web-ai driver (SITES) plus the site() lookup.

Split out of webchat.py (2026-08-07, Task #1011) so the driver entry stays
under the 800-line hard ceiling.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Per-site profiles. Each value is a list of CSS selectors tried in order; the
# first that matches wins, so the most-specific/current selector goes first and
# older fallbacks follow. To repair a drifted site, prepend the new selector.
# --------------------------------------------------------------------------- #

SITES: dict[str, dict[str, Any]] = {
    "chatgpt": {
        "label": "ChatGPT",
        "new_chat_url": "https://chatgpt.com/",
        "host": "chatgpt.com",
        # Login detection: text on the page that indicates a sign-in wall.
        "login_indicators": ["Log in to ChatGPT", "Sign up", "Welcome back"],
        # Click paths that attempt auto-login via "Continue with Google" (the
        # shared Chrome is already signed into a Google account, so this is
        # often a single click).
        "auto_login": [["Continue with Google"], ["Log in with Google"], ["Sign in with Google"]],
        # Chat ID: extracted from the conversation URL after the first message.
        "chat_id_patterns": [r"/c/([a-zA-Z0-9_-]+)"],
        "chat_url_template": "https://chatgpt.com/c/{chat_id}",
        "composer": [
            "#prompt-textarea",
            'div[contenteditable="true"]#prompt-textarea',
            "textarea#prompt-textarea",
            'main div[contenteditable="true"]',
        ],
        "send": [
            'button[data-testid="send-button"]',
            "#composer-submit-button",
            'button[aria-label="Send prompt"]',
        ],
        "stop": [
            'button[data-testid="stop-button"]',
            'button[aria-label="Stop streaming"]',
        ],
        "answer": ['[data-message-author-role="assistant"]'],
        # Click path to the control that opens the OS file chooser (the final
        # step is handed the file instead of a plain click).
        "attach_path": [["Add files and more"], ["Add photos & files"]],
    },
    "gemini": {
        "label": "Gemini",
        "new_chat_url": "https://gemini.google.com/app",
        "host": "gemini.google.com",
        "login_indicators": ["Sign in", "Sign in to continue", "Choose an account"],
        "auto_login": [["Continue with Google"], ["Sign in with Google"]],
        "chat_id_patterns": [r"/app/([a-zA-Z0-9_-]+)"],
        "chat_url_template": "https://gemini.google.com/app/{chat_id}",
        "composer": [
            'div.ql-editor[contenteditable="true"]',
            'rich-textarea div[contenteditable="true"]',
            'div[role="textbox"][contenteditable="true"]',
        ],
        "send": [
            'button[aria-label="Send message"]',
            "button.send-button",
            'button[mattooltip="Send message"]',
        ],
        "stop": [
            'button[aria-label="Stop response"]',
            'button[aria-label*="Stop"]',
        ],
        "answer": [
            "message-content .model-response-text",
            "message-content",
            ".model-response-text",
        ],
        "attach_path": [["Upload & tools"], ["Upload files"]],
    },
    "claude": {
        "label": "Claude",
        "new_chat_url": "https://claude.ai/new",
        "host": "claude.ai",
        "login_indicators": ["Log in", "Sign up", "Sign in", "Welcome to Claude"],
        "auto_login": [["Continue with Google"], ["Log in with Google"], ["Sign in with Google"]],
        "chat_id_patterns": [r"/chat/([a-zA-Z0-9_-]+)"],
        "chat_url_template": "https://claude.ai/chat/{chat_id}",
        "composer": [
            'div.ProseMirror[contenteditable="true"]',
            'div[aria-label="Write your prompt to Claude"] div[contenteditable="true"]',
            'div[contenteditable="true"][translate="no"]',
        ],
        "send": [
            'button[aria-label="Send message"]',
            'button[aria-label="Send Message"]',
            'button[aria-label*="Send"]',
        ],
        "stop": [
            '[data-is-streaming="true"]',
            'button[aria-label="Stop response"]',
            'button[aria-label*="Stop"]',
        ],
        # Read the visible rendered markdown (.standard-markdown), last turn. The
        # old screen-reader heading ("Claude responded: ...") is no longer a
        # verbatim mirror — it now holds a short AI summary, so reading it
        # truncated every answer to ~a sentence. innerText of the rendered markup
        # would lose ordered-list numbers (they are ::marker pseudo-elements, not
        # text — e.g. a bare "1969." renders as <ol start="1969"> with an empty
        # li), so reconstruct ol/ul markers on a detached clone, then read its
        # innerText off-screen (innerText needs layout, so the clone is briefly
        # attached and removed). During streaming this grows in step with the
        # answer, so wait_until_idle settles on the real text, not a placeholder.
        "answer_js": (
            "() => {"
            "  const els = document.querySelectorAll('.standard-markdown');"
            "  if (!els.length) return '';"
            "  const clone = els[els.length - 1].cloneNode(true);"
            "  for (const ol of clone.querySelectorAll('ol')) {"
            "    let n = parseInt(ol.getAttribute('start') || '1', 10);"
            "    for (const li of [...ol.children]) {"
            "      if (li.tagName !== 'LI') continue;"
            "      const v = li.getAttribute('value');"
            "      if (v !== null && v !== '') n = parseInt(v, 10);"
            "      li.insertBefore(document.createTextNode(n + '. '), li.firstChild);"
            "      n++;"
            "    }"
            "  }"
            "  for (const ul of clone.querySelectorAll('ul')) {"
            "    for (const li of [...ul.children]) {"
            "      if (li.tagName === 'LI') li.insertBefore(document.createTextNode('- '), li.firstChild);"
            "    }"
            "  }"
            "  clone.style.position = 'fixed';"
            "  clone.style.left = '-99999px';"
            "  clone.style.top = '0';"
            "  document.body.appendChild(clone);"
            "  const text = clone.innerText;"
            "  clone.remove();"
            "  return text;"
            "}"
        ),
        "attach_path": [["Add files, connectors, and more"], ["Add files or photos"]],
    },
    "perplexity": {
        "label": "Perplexity",
        "new_chat_url": "https://www.perplexity.ai/",
        "host": "www.perplexity.ai",
        "login_indicators": ["Log in", "Sign up", "Sign in", "Welcome to Perplexity"],
        "auto_login": [["Continue with Google"], ["Log in with Google"], ["Sign in with Google"]],
        "chat_id_patterns": [r"/search/([a-zA-Z0-9_-]+)", r"/thread/([a-zA-Z0-9_-]+)"],
        "chat_url_template": "https://www.perplexity.ai/search/{chat_id}",
        "composer": [
            "#ask-input",
            'div[contenteditable="true"]#ask-input',
            '[role="textbox"]#ask-input',
        ],
        "send": ['button[aria-label="Submit"]'],
        "stop": [
            'button[aria-label^="Stop response"]',
            'button[aria-label*="Stop"]',
        ],
        # Each answer renders as markdown into a div id'd markdown-content-<n> (n
        # grows one per turn) — the selector-list reader takes the last match, so
        # that is the newest answer. .prose is the same block's typography wrapper,
        # kept as a fallback if the id scheme drifts.
        "answer": ['[id^="markdown-content"]', "div.prose"],
        # Attach is two steps: "Add files or tools" opens a popup menu (it ignores
        # synthetic clicks — the real-input-event click_by_text is required), then
        # its "Upload files or images" item opens the OS file chooser (handed the
        # file). The input accepts .pdf/.docx/.csv/.png/... and many more.
        "attach_path": [["Add files or tools"], ["Upload files or images"]],
    },
}


def site(name: str) -> dict[str, Any]:
    """Return the profile for `name`, or raise listing the known sites."""
    try:
        return SITES[name]
    except KeyError:
        raise ValueError(f"unknown site {name!r}; known: {', '.join(SITES)}") from None
