```markdown
# starters/react_vite

Vite + React 19 + TypeScript starter. Suitable for complex layouts / multi-component / state management. Dev server includes HMR (direct same-origin WebSocket).

## How to use

```python
import shutil
shutil.copytree(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/starters/react_vite", "/tmp/my-app", dirs_exist_ok=True)

# Copy widget into components/
import os
os.makedirs('/tmp/my-app/src/components', exist_ok=True)
shutil.copy(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/widgets/markdown/Markdown.tsx", "/tmp/my-app/src/components/")

# Install dependencies (widget READMEs list extra deps). install may take tens of seconds, set a higher timeout.
ava.shell.run('cd /tmp/my-app && npm install', timeout=600)
ava.shell.run('cd /tmp/my-app && npm install react-markdown remark-math rehype-katex remark-gfm rehype-highlight katex highlight.js', timeout=600)

# Edit src/App.tsx to use the widget

# Start dev server (background shell session, runs until agent exits). --host 0.0.0.0 makes
# it accessible from the user's browser.
sess = ava.shell.sessions.new("my-app")
ava.shell.sessions.send(sess, 'cd /tmp/my-app && npm run dev -- --port 5173 --host 0.0.0.0')

# Vite dev server takes 1-3s to bind port. sessions.send does not wait, must poll —
# otherwise after registering, the first open of the page URL will fail to connect, until Vite is ready.
import time, urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen('http://127.0.0.1:5173/', timeout=1).read()
        break
    except Exception:
        time.sleep(0.3)
else:
    raise RuntimeError('vite dev server failed to come up in 18s')

# Register with gateway
page = ava.ui.show('my-react-page', 5173, title='My React Page')
print(f'preview: {page.url}')
```

## HMR

page directly connects to the server (no gateway reverse proxy), HMR goes through same-origin WebSocket, no need for special clientPort / protocol configuration. See `vite.config.ts`.

## prod build

```bash
npm run build  # → dist/
# Then you can also serve dist/ using `python -m http.server` (no longer need vite dev server)
```

## Known pitfalls

- **CSS import**: widget uses KaTeX / highlight.js CSS, at the top of `src/main.tsx`
  just `import 'katex/dist/katex.min.css'` once is enough.
```
