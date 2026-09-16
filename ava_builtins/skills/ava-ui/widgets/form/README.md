# form — labeled fields → reply

Collect structured input: a list of fields, the user fills and submits, and the
values go back to the agent via the [ava_reply](../ava_reply/README.md) spine as a
`label: value` block (one line per field). Use when a single choice isn't enough.

Fill in `form.html`:
- `TITLE` — heading.
- `FIELDS` — `[{ name, label, type, options?, required?, placeholder? }]`; `type`
  is `text` | `textarea` | `select` (`options=[{value,label}]` for select).
- `SUBMIT_LABEL` — button text.
- the three `AVA_*` placeholders (see [ava_reply](../ava_reply/README.md)).

Zero build: serve the directory with `ava.ui.serve(dir, name, port)` (starts the server + `/health` + registration in one call).
