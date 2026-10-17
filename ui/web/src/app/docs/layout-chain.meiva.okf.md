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

```meiva-contract
id = "stack-wrapper-keeps-the-chain"
kind = "invariant"
statement = "The page stacks the permission banner above the shell, and both wrapper layers carry FLEX explicitly: the outer one as `flex flex-col` (banner row plus shell row) and the inner one as `flex` (HomeLayout's narrow branch is a fragment whose children are flex items of it). A wrapper built from FLEX_COL alone is display:block and the chain collapses to content height -- measured 2026-10-17 at 320/390/768: the timeline surface grew to 4920px inside a 664px viewport, I6 failed, and the composer was pushed off screen. The banner adds one row when the host is restricted and renders nothing otherwise, so the geometry with no banner is the pre-banner geometry."
code = ["ui/web/src/app/page.tsx:HomePage"]
tests = ["tests/e2e/visual/test_layout_invariants.py::test_timeline_layout_invariants"]
```
