---
type: doc
title: To user resources
description: Serving a referenced workspace file — disposition, CSP sandbox and the HTML isolation boundary.
tags: [gateway, to-user, security]
---

# To user resources

`GET /api/agents/{id}/to-user/resource` streams one file the panel's Markdown
references, after the reference chain is re-read on the owning machine. The last
hop is `resource_response`, which owns both the content disposition and the CSP a
browser applies to the bytes.

## Disposition

Readability decides, never the file extension alone. Media players, the PDF
viewer and inert text render inline; archives, office documents, executables,
scripts and unknown types stay attachments so the browser never hands them to a
plugin or the desktop. Markdown itself renders in the panel, so its resource form
is the raw source a modifier-click opens.

## The HTML boundary

A report page that only downloads is useless, so HTML renders inline. The price is
that an HTML document is active content served from the gateway origin, which is
the application's own origin: without a sandbox its script would read the session
cookie, `localStorage` and the authenticated API exactly as the app does — agent
-authored HTML would become an XSS channel into the app.

`sandbox` without `allow-same-origin` gives the document a unique opaque origin
instead: origin reads, cookie reads and `localStorage` all raise `SecurityError`,
and CSP `connect-src 'none'` (inherited from `default-src`) blocks every fetch
back into the API. `allow-scripts` stays because workspace reports draw their own
charts; it is safe only while the origin stays opaque, and the two flags together
would let the page delete its own sandbox attribute and reload itself into the app
origin. No change may add `allow-same-origin`.

The page may load `https:` and `data:` images, media, fonts, styles and scripts;
inline styles and scripts are allowed because the document already owns its own
content. Forms, `<base>`, frames, popups and top-level navigation remain closed.
A relative path inside the page cannot resolve — the endpoint has no directory —
so a report using local assets must inline them or use absolute URLs.

A referenced HTML document renders inline under a script-only CSP sandbox and never `allow-same-origin`, so its scripts run in an opaque origin that cannot read the gateway's cookies, localStorage or authenticated API; media, PDFs and inert text render under the plain sandbox, while archives, office documents, executables and unknown types stay attachments.
