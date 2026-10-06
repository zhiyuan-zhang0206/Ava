# Single HTML starter

Copy this directory to a task-owned page directory and edit `index.html`.
The installed source is `$AVA_HOME/skills/ava-guide/pages/starters/single_html/`.
Use an installed frontend skill for design decisions.

Read `ava.help(ava.ui.serve)` and publish the directory. Open the returned page
URL, not the raw server port. Relative asset and page links resolve inside the
registered page: `images/example.png` or `other.html`, rather than `/images/...`.

Copy widget dependencies together with the widget. The Markdown renderer ships
its own `vendor/` directory; confirm and compare need `widgets/vendor/purify.min.js`.
Adjust their script path if you move the widget to `index.html`.
For input, follow the [reply resource](../../widgets/ava_reply/README.md).
