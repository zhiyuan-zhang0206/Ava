---
type: doc
title: Code blocks
description: PythonCode highlighter bundle strategy — lazy import out of the home route, proactive warm-up and prefetch triggers, and when tokenization runs.
tags: [frontend]
---

# Code blocks

`PythonCode` keeps its highlighter (`prism-react-renderer`, ~85KB) out of
the `/` route's initial bundle via a plain dynamic `import()`, but warms
that chunk proactively: `CodeHighlighterPreloader` mounts on every page
with code blocks (home timeline, `/control/schedules`) and fetches it once
idle; a code block's own collapsed toggle (`CardHeader`, the schedules row
expand button) also fetches it on pointer-enter/focus, ahead of the click
that expands it. Not yet resolved → plain unhighlighted text (copy control
+ streaming cursor still present, no blank body); already prefetched → the
first render is already highlighted, no flash. Tokenization still only
runs when a code block renders or its source changes.

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
