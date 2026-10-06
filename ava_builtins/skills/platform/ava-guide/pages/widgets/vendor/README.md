# widgets/vendor

Shared vendor libraries for UI widgets. Keep these local — no CDN.

| File | Version | Used by |
|---|---|---|
| `purify.min.js` | 3.2.3 | compare, confirm (and markdown has its own copy) |

## Usage

When you copy a widget into your page directory, also copy the vendor files
it needs. The widgets reference them as `../vendor/purify.min.js` relative
to the widget directory — adjust the path if your page layout differs.

Copy the installed widget from
`$AVA_HOME/skills/ava-guide/pages/widgets/compare/compare.html` to your
page's `index.html`. Copy `widgets/vendor/purify.min.js` beside it and change
`../vendor/purify.min.js` to `./purify.min.js` in the page.
