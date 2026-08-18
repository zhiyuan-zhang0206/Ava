# widgets/markdown

Render Markdown with **LaTeX formulas** + **code highlighting** + GFM tables + relative path images.

## Why encapsulate

Using `marked.js` or `react-markdown` alone is not difficult, but to configure both **KaTeX for rendering LaTeX formulas** and **highlight.js for code highlighting** at the same time, agents easily miss steps or install dependencies in the wrong order (math must be applied before highlight, and the timing of KaTeX auto-render is also a pitfall). This is essential for long articles with mathematical formulas on Zhihu columns / answers / pins.

## Two versions

### HTML version (`md.html`)

Zero build. Paste the whole file into a single page for use. Dependencies (marked / DOMPurify / KaTeX / highlight.js, about 750KB) are all vendored in `vendor/`, no CDN pulling — when starting a server, copy the `vendor/` directory together to the page directory.

```python
# agent flow: serve() starts a static server and registers it, done in one line
ava.shell.run('mkdir -p /tmp/my-page && cp -r $AVA_HOME/skills/ava-ui/widgets/markdown/{md.html,vendor} /tmp/my-page/ && mv /tmp/my-page/md.html /tmp/my-page/index.html')
# Edit /tmp/my-page/index.html, replace the {{MARKDOWN_CONTENT}} placeholder
# inside the <script type="text/markdown" id="md-source"> with the content you want to render
page = ava.ui.serve('/tmp/my-page', 'zhihu-answer', 8765)
```

If you need to dynamically inject Markdown (read from file):

```python
template = Path('/tmp/my-page/index.html').read_text()
md_source = (outdir / 'post.md').read_text()
# escape `</script>` for safe embedding
md_safe = md_source.replace('</script>', '<\\/script>')
html = template.replace('{{MARKDOWN_CONTENT}}', md_safe)
```

### React version (`Markdown.tsx`)

Suitable for projects started with `starters/react_vite`. Paste into `src/components/` and import to use.

```bash
npm i react-markdown remark-math rehype-katex remark-gfm rehype-highlight katex highlight.js
```

```tsx
import { Markdown } from './components/Markdown';
import 'katex/dist/katex.min.css';
import 'highlight.js/styles/github.css';

export default function App() {
  return <Markdown source={mdString} />;
}
```

## Conventions

**None**. The widget does not constrain what the outdir looks like — agents read the md file themselves (using `ava.files.read` / `bash cat`) and inject the string. Image paths use `![alt](images/1.jpg)` inside markdown, which by default resolves to the page-server root, so when starting the server, the agent just needs to cd to the directory containing `images/`.

## Known limitations

- **Mermaid / PlantUML diagrams**: Currently not supported; add `mermaid.js` CDN to handle it yourself.
- **Table of Contents (TOC)**: Use `marked.lexer()` to extract headings and assemble your own.
- **Editable mode (contenteditable / monaco)**: Currently read-only; not in the scope of this widget.
