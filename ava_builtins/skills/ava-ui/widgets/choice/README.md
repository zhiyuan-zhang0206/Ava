# choice — single / multi select → reply

Present N options, the user picks (radio = one, checkbox = many) and confirms;
the page sends `choice: <labels>` back to the agent via the [ava_reply](../ava_reply/README.md)
spine. This is the building block for "ask the human to pick" — including a
reduce point's decision (see also [compare](../compare/README.md) for side-by-side artifacts).

Fill in `choice.html`:
- `TITLE` — the question.
- `MULTI` — `false` (pick one) or `true` (pick any number).
- `OPTIONS` — `[{ value, label, detail? }]`.
- the three `AVA_*` placeholders (see [ava_reply](../ava_reply/README.md)).

Zero build: `python -m http.server`, then `ava.ui.show(name, port)`. Whole file
as `index.html`, or embed the `<style>` + `<div id="choice">` + both `<script>`
blocks into your own page.
