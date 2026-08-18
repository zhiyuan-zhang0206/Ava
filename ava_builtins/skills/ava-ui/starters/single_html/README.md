```markdown
# starters/single_html

Zero build, one `index.html` + `python -m http.server`. Agent writes a one-off / simple display page, enough for 90% of scenarios.

## How to use

```python
import shutil
shutil.copytree(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/starters/single_html", "/tmp/my-page", dirs_exist_ok=True)

# Edit index.html — paste widget content / change placeholder / etc.
# (use ava.files.write or bash > overwrite the entire file)

# Start a persistent shell session to run the server (background session, keep running until agent exits)
sess = ava.shell.sessions.new("my-page")
ava.shell.sessions.send(sess, 'cd /tmp/my-page && python -m http.server 8765')

# poll until port is listening (sessions.send is fire-and-forget, doesn't wait for bind)
import time, urllib.request
for _ in range(30):
    try:
        urllib.request.urlopen('http://127.0.0.1:8765/', timeout=1).read()
        break
    except Exception:
        time.sleep(0.2)

# Register
page = ava.ui.show('my-page', 8765, title='My Page')
print(f'preview: {page.url}')
```

## Working with widgets

Each widget's HTML version is designed to be directly pasted into the `<body>` of `index.html`, and if the widget has CDN dependencies (marked.js / KaTeX / etc), add the corresponding `<link>` / `<script src>` in the `<head>`.

## Multiple pages

Need `index.html` + `other.html` + subdirectories? `python -m http.server` already serves the entire cwd (root path `/`), just put them there. The URL is `<page.url>other.html` (page.url is `http://<host>:<port>/`).

## Unsuitable scenarios

- Complex state management (React is smoother): use `starters/react_vite/`
- Multi-component reuse: use `starters/react_vite/`
- Want to use Tailwind / shadcn / existing component lib: use `starters/react_vite/`
- Needs SSR / API routes: start your own Next.js (no starter, add as needed)
```
