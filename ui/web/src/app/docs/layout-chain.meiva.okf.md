---
type: doc
title: Home shell layout chain
description: "Why the permission banner's wrapper must stay a flex row container: the primitives are split, the narrow shell is a fragment, and a block wrapper collapses the height chain the timeline scrolls inside."
tags:
- ui
- layout
---

# Home shell layout chain

`ui/web/src/lib/layout/layout.ts` splits the flex primitives: `FLEX` is
`flex`, `FLEX_COL` is `flex-col` only. A wrapper that stacks the banner above the
shell therefore needs **both**; `FLEX_COL` alone leaves `display: block`, and a
block's height is its content height, so nothing below can shrink.

`HomeLayout`'s narrow branch returns a fragment (`sidebar`, `main`, `inspector`)
whose children are flex items of *their parent* -- so the wrapper the page puts
around the shell must stay a row.
