---
name: ava-ui
description: Serves web pages that display rich content or collect user choices, confirmations, forms, and comparisons. Use when markdown, LaTeX, transcripts, visual comparisons, or interactive input would be clearer in a browser page.
---

# ui

Provides **frontend boilerplate** for the agent to spin up a page for the user to view.
**No Python writing**, no Python API. Everything is HTML / JS / TSX / starter
projects; the agent uses the file tools `read` / `cp` to fetch them, assemble and edit them, then start a server.

`ava.ui.serve(dir, name, port=None)` is the one-call path for static files: it starts
a `ThreadingTCPServer` on `0.0.0.0:<port>` with a `/health` endpoint, polls until
listening, and registers the page — the whole session-start + poll + show dance
in one call. The server handles concurrent requests and includes basic error
recovery for dropped connections. `port` defaults to a port reserved for your agent;
pass one to override. If the port is already in use by another process, serve
**raises** instead of killing the occupant — see Troubleshooting for the re-serve recipe.

`serve()` is a **static file server** — it hands the browser the file bytes as-is,
it does not render anything. A `.md` file therefore opens as **raw Markdown
source** (`#`, `**`, table pipes interleaved with your prose), which reads as
garbled to the user.

> ⚠️ **Never serve a `.md` file directly.** The user will see raw markup, not a
> rendered page. Always render Markdown to HTML before serving.

**To serve Markdown as a rendered page**, use `ava.ui.serve_markdown()` —
the one-call path that handles the whole widget dance for you:

```python
page = ava.ui.serve_markdown(md_string, "my-report", 8765)
```

If you need more control (multiple pages, custom HTML wrapper), use the
[markdown widget](widgets/markdown/README.md) directly — paste your
content into `md.html`'s `{{MARKDOWN_CONTENT}}` slot, save it as `index.html`, copy
the widget's `vendor/` alongside — then `serve()` that directory.

`ava.ui.show(name, port=None)` is the underlying transport: you start an HTTP server
yourself (bound on `0.0.0.0`) and register it with the gateway (`port` defaults to
the one reserved for your agent; show does not check whether it is free). Use this when you
need a non-`http.server` process — e.g. `npm run dev`, a custom Python server, or
a server you already started. The registered URL is the **direct**
`http://<host>:<port>/` the user opens in a new tab over the shared trusted
network. The gateway only keeps the registry — there is no reverse proxy, your
server is served at its own root `/`.

On top of that, this skill provides:
- **widget**: a couple that are annoying for the agent to write itself (markdown with
  LaTeX/code blocks/images; transcript synced with audio/video timing).
- **starter**: complete starter projects (single_html zero build; react_vite Vite + React).

Things like a video player / image / image-gallery that one line of HTML can handle are **not provided** —
the agent just writes `<video src=...>` `<img src=...>` itself.

## Sending a decision back from the page (interactive panels)

Pages are not display-only — they can send something back: a button click, a
selection, a confirmation. The page's JS POSTs **straight to the gateway's
message endpoint**, which delivers it as an inbound to whichever agent you
target:

    POST {GATEWAY_URL}/api/agents/{AGENT_ID}/messages
    body: {"content": "...", "source": "ui:page:<your-page-name>"}

This works cross-origin (the gateway allows any origin) and needs **no auth** —
the private network is the trust boundary, the same model as the rest of the gateway, so
do not treat this endpoint as an authorization check. Template your gateway base
URL and the target agent id into the page when you write it; the agent knows
both — `ava.GATEWAY_URL` and `ava.self.AGENT_ID` (target yourself to wake on the
decision, or another agent to report to it). Set `source="ui:page:<name>"` so
the inbound is attributed to the page.

```js
// in the page's JS, on confirm:
await fetch(`${GATEWAY_URL}/api/agents/${AGENT_ID}/messages`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ content: "chose: option B", source: "ui:page:compare" }),
});
```

After registering the page, the page's POST wakes you with the
decision as an inbound, the same as any message.

## Tutorial — hello-world: spin up a display page

### Recommended: one call with `serve`

```python
import ava

# 1. Write a minimal frontend
ava.shell.run('mkdir -p /tmp/hello-ui', persistent=False)
ava.files.write('/tmp/hello-ui/index.html', '''
<!DOCTYPE html>
<html><body>
  <h1>Hello from agent</h1>
  <p>This page is served straight from the agent runner.</p>
</body></html>
''')

# 2. One call: start server, poll until ready, register. Done.
page = ava.ui.serve('/tmp/hello-ui', 'hello', 8765, title='Hello UI')
print(f'page at: {page.url}')   # http://<host>:8765/

# 3. The user sees 'Hello UI' in the Pages popover, opens it -> sees the page.
```

### Manual: when you control the server process yourself

Use this when you need a non-`http.server` process (`npm run dev`, custom
server) or want to keep the server session handle for later management.

```python
import ava
import time, urllib.request

# 1. Write a minimal frontend
ava.shell.run('mkdir -p /tmp/hello-ui', persistent=False)
ava.files.write('/tmp/hello-ui/index.html', '''
<!DOCTYPE html>
<html><body>
  <h1>Hello from agent</h1>
  <p>This page is served straight from the agent runner.</p>
</body></html>
''')

# 2. Start the server in a background session (TTL is mandatory; 24h covers the page's default lifetime)
session = ava.shell.sessions.new("my-server", ttl=24 * 3600)
ava.shell.sessions.send(session, 'cd /tmp/hello-ui && python -m http.server 8765')

# 3. Poll until the server is really listening
for _ in range(30):
    try:
        urllib.request.urlopen('http://127.0.0.1:8765/', timeout=1).read()
        break
    except Exception:
        time.sleep(0.2)
else:
    raise RuntimeError('hello-ui server failed to come up in 6s')

# 4. Register with the gateway
page = ava.ui.show('hello', 8765, title='Hello UI')
print(f'page at: {page.url}')   # http://<host>:8765/
```

## Catalog

### widgets/ — display (read-only, two versions: HTML + React)

| widget | Does what | Files |
|---|---|---|
| [markdown/](widgets/markdown/README.md) | render Markdown with LaTeX ($...$/$$...$$) + code highlighting + images + tables | `md.html` + `Markdown.tsx` |
| [transcript/](widgets/transcript/README.md) | SRT/VTT transcript, highlights the current line in sync with the audio/video element `timeupdate` event | `transcript.html` + `Transcript.tsx` |

### widgets/ — interactive (page sends a result back to the agent)

These build on **[ava_reply](widgets/ava_reply/README.md)** — the one shared
spine that POSTs the page's result to the agent as an inbound (the agent went
idle after `ava.ui.show` and wakes on it). Each widget embeds the spine inline;
fill the three `AVA_*` placeholders (`ava.GATEWAY_URL`, `ava.self.AGENT_ID`, the
page name) when you write the page. Single-file, zero build. (Display-only pages
don't need any of this.)

| widget | Does what | Files |
|---|---|---|
| [ava_reply/](widgets/ava_reply/README.md) | the callback spine: `avaReply(content)` → inbound to the agent. The others embed it | `reply.js` |
| [choice/](widgets/choice/README.md) | single / multi select of N options + confirm → `choice: <labels>` | `choice.html` |
| [confirm/](widgets/confirm/README.md) | judgment gate: context + approve/reject (+ note) → `approved`/`rejected` | `confirm.html` |
| [form/](widgets/form/README.md) | labeled fields (text/textarea/select) → submit → `label: value` block | `form.html` |
| [compare/](widgets/compare/README.md) | N artifacts side by side + pick one → `picked: <label>` — the reduce-point panel | `compare.html` |

### starters/ — starter project templates

| starter | Suits | Files |
|---|---|---|
| [single_html/](starters/single_html/README.md) | Zero build, one index.html + `python -m http.server`. Enough for 90% of simple-page cases the agent writes | `index.html` + `README.md` |
| [react_vite/](starters/react_vite/README.md) | Vite + React, npm run dev hot reload. Use for complex layouts / multiple components / state management | the whole Vite project structure |

## Usage pattern

**Simple one-off page (single_html starter, plain static files) — use `serve`**:
1. `cp -r "$AVA_HOME/skills/ava-ui/starters/"single_html /tmp/<your-name>`
2. Edit `/tmp/<your-name>/index.html`, paste the widget content in (per the widget README)
3. `page = ava.ui.serve('/tmp/<your-name>', '<name>', <port>)` — that is it.

**With build / multiple pages / complex layout (react_vite) — manual**:
1. `cp -r "$AVA_HOME/skills/ava-ui/starters/"react_vite /tmp/<your-name>`
2. `ava.shell.run('cd /tmp/<your-name> && npm install', timeout=600)`
3. paste a React widget (`Markdown.tsx` / `Transcript.tsx` / etc.) into `src/components/`
4. import and use it in `src/App.tsx`
5. `sess = ava.shell.sessions.new("dev-server", ttl=24 * 3600); ava.shell.sessions.send(sess, 'cd /tmp/<your-name> && npm run dev -- --port <port> --host 0.0.0.0')`
6. **poll the port for readiness** (npm run dev takes 1-3s to come up), then `page = ava.ui.show('<name>', <port>)`

## Troubleshooting

- **Page URL does not open / connection refused**: the server is not actually listening, or
  it bound `127.0.0.1` instead of `0.0.0.0` (then only the agent runner itself can reach it, not
  the user's browser). Bind `0.0.0.0`. Check the server with `ava.shell.sessions.list()`
  + `capture(id)`; `show()` returns the registered Page (with its URL) on success, and the
  frontend Pages popover lists every open page.
- **markdown widget LaTeX not rendering**: all dependencies (marked.js, KaTeX, highlight.js,
  DOMPurify) are vendored locally in `widgets/markdown/vendor/` — no CDN needed. If LaTeX does
  not render, check that the vendor files were copied alongside the widget HTML and that the
  `<script>` / `<link>` paths resolve correctly.
- **compare / confirm widget security warning**: DOMPurify is vendored in
  `widgets/vendor/purify.min.js` — copy it alongside your page. No CDN required.
- **server hangs / unresponsive**: the server uses `ThreadingTCPServer` so one slow request
  does not block others. If the server process is stuck, call `ava.ui.close(name)` then
  re-`serve()`. serve does **not** kill whatever holds the port — if an old server is still
  bound (e.g. it was started in a shell session that outlived the process, so `close` no
  longer tracks it), serve raises. Stop that session first: `ava.shell.sessions.list()`,
  then `ava.shell.sessions.kill(id)` — or re-serve on a different `port`.
