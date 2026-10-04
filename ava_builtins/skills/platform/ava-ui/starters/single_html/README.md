```markdown
# starters/single_html

Zero build, one `index.html` served with `ava.ui.serve()`. Agent writes a one-off / simple display page, enough for 90% of scenarios.

## How to use

```python
import shutil
shutil.copytree(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/starters/single_html", "/tmp/my-page", dirs_exist_ok=True)

# Edit index.html — paste widget content / change placeholder / etc.
# (use ava.files.write or bash > overwrite the entire file)

# Serve the directory and register the page in one call — `serve` starts the server
# (answering `/health` for the platform probe), polls until ready, and registers.
page = ava.ui.serve('/tmp/my-page', 'my-page', 8765, title='My Page')
print(f'preview: {page.url}')
```

## Working with widgets

Each widget's HTML version is designed to be directly pasted into the `<body>` of `index.html`, and if the widget has CDN dependencies (marked.js / KaTeX / etc), add the corresponding `<link>` / `<script src>` in the `<head>`.

## Multiple pages

Need `index.html` + `other.html` + subdirectories? `serve()` serves the whole directory tree at root `/` — just put them there. The URL is `<page.url>other.html` (page.url is `http://<host>:<port>/`).

## Unsuitable scenarios

- Complex state management (React is smoother): use `starters/react_vite/`
- Multi-component reuse: use `starters/react_vite/`
- Want to use Tailwind / shadcn / existing component lib: use `starters/react_vite/`
- Needs SSR / API routes: start your own Next.js (no starter, add as needed)
```
