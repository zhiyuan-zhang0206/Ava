# Source notes and file links replace typed document records

This supersedes
[structured comments with colocated documentation](2026-10-15-structured-code-comments.md).

## Decision and scope

An explanation now lives in exactly one place. When one code site needs it, it is
a `meiva:note` block at that site: a parseable comment block with plain text and
zero or more `meiva:link` lines naming tracked repository files. When several
sites share it, it stays in the component document as prose and each site links
to that file.

The typed document records are gone: no `meiva-contract` fences, no `id`,
`kind`, `code`, `tests`, `api` or `decision` fields, no `render-api` projection.
Agent-visible SDK help is the docstring itself, edited in place, with the
existing `agent_docstrings.py` content rules.

The whole corpus moved in one change. A compatibility window would have kept two
authoring forms alive for months while the repository kept growing at roughly a
hundred records a day.

## What the record layer cost

Measured on the corpus this replaces (265 records in 121 documents):

- `kind` carried almost no information: 254 were `invariant`; `external`,
  `decision` and `api` accounted for 11.
- Text length and arity already decided placement: 80% of statements exceeded a
  one-line comment, and 130 records (129 `invariant`, one `external`) bound 2 to
  12 definitions. Inlining those
  would have duplicated the same explanation across sites, which is the rot the
  policy exists to prevent.
- `tests` anchors restated ownership the code and the review already carry, and
  every one of them had to be re-proved on each move.
- SDK help had two owners: a record and the docstring generated from it.

## Alternatives and trade-offs

- Keep records with fewer fields. Rejected: any field set keeps a second copy of
  text whose value is already available at the code site, and the classification
  had no reader.
- Inline every record, including multi-site ones. Rejected: it duplicates text by
  construction, and the duplicate drifts.
- Migrate incrementally, converting only touched files. Rejected: it leaves two
  authoring forms and an unbounded compatibility surface; the conversion is
  mechanical, so one change was reviewable.
- Keep URL and test-node links. Rejected as unused surface: the corpus needed file
  navigation, and four external constraints fit in the note text.
- Keep a generated SDK projection. Rejected: `meiva.help` already reads
  docstrings, so the record was a second owner of one text.

## Consequences

- A link is coarse: it names a file, not a section. Documents are small and
  topical, so the reader lands on the right page and searches.
- No machine check proves a note is true or still describes the code. The corpus
  extract gives review a list instead of a diff to scan; semantic truth stays a
  reviewer judgment.
- Nothing enforces that an agent-visible name has a docstring. The content rules
  exist, the coverage check does not; that gap predates this change and is
  tracked separately from the policy.
- `changed` still gates only new or rewritten notes, with every link validated.
  The strict full-tree inventory remains available and explicit.

Current instructions live in
[structured code comments](../../../conventions/engineering/structured-code-comments.md).
