# widgets/vendor

Shared vendor libraries for UI widgets. Keep these local — no CDN.

| File | Version | Used by |
|---|---|---|
| `purify.min.js` | 3.2.3 | compare, confirm (and markdown has its own copy) |

## Usage

When you copy a widget into your page directory, also copy the vendor files
it needs. The widgets reference them as `../vendor/purify.min.js` relative
to the widget directory — adjust the path if your page layout differs.

```python
import shutil
# Copy the widget
shutil.copy(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/widgets/compare/compare.html", "/tmp/my-page/index.html")
# Copy the vendor file it needs
shutil.copy(f"{{os.environ['AVA_HOME']}}/skills/ava-ui/widgets/vendor/purify.min.js", "/tmp/my-page/purify.min.js")
# Then edit index.html to point to ./purify.min.js instead of ../vendor/...
```
