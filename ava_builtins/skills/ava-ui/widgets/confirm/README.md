# confirm — approve / reject a step → reply

A judgment gate: show context, the user approves or rejects (with an optional
note), and the verdict goes back to the agent via the [ava_reply](../ava_reply/README.md)
spine. Use when a reduce point or an irreversible step needs an explicit human
yes/no.

Fill in `confirm.html`:
- `TITLE` / `BODY_HTML` — the context to judge. `BODY_HTML` is rendered as
  DOMPurify-sanitized HTML (rich formatting kept; scripts / event handlers
  stripped), so artifact text carrying untrusted content is safe by default.
- `APPROVE_LABEL` / `REJECT_LABEL` — button text.
- the three `AVA_*` placeholders (see [ava_reply](../ava_reply/README.md)).

Sends `approved: <note>` or `rejected: <note>`. Zero build: `python -m http.server`
then `ava.ui.show(name, port)`.


## Dependencies

- `../vendor/purify.min.js` — DOMPurify for sanitizing body HTML. Copy
  `widgets/vendor/` alongside your page, or embed the script inline.
