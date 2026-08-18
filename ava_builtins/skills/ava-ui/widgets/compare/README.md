# compare — N artifacts side by side → reply

The reduce-point panel: render N candidate artifacts as side-by-side panes, the
user picks one and confirms, and the choice goes back to the agent via the
[ava_reply](../ava_reply/README.md) spine. This is [choice](../choice/README.md)
with each option shown as a full pane — the turnkey form of a manager presenting
a reduce decision to the human.

Fill in `compare.html`:
- `TITLE` — the decision.
- `PANES` — `[{ label, html }]`; `label` is the pick value, `html` is the pane
  body (paste a rendered diff / markdown / summary). It is rendered as
  DOMPurify-sanitized HTML (rich formatting kept; scripts / event handlers
  stripped), so untrusted artifact content is safe by default.
- the three `AVA_*` placeholders (see [ava_reply](../ava_reply/README.md)).

Sends `picked: <label>`. Zero build: `python -m http.server` then
`ava.ui.show(name, port)`. The human reaches it from the chat Pages popover or
the `/fleet` per-row panel button.


## Dependencies

- `../vendor/purify.min.js` — DOMPurify for sanitizing pane HTML. Copy
  `widgets/vendor/` alongside your page, or embed the script inline.
