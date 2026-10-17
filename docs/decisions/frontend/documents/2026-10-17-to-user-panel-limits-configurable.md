# To user panel read limits become config with widened defaults

## Context

The shared-context panel's first version ([2026-10-15
 decision](2026-10-15-markdown-context-and-file-references.md)) hardcoded four
read-side bounds: 256 KiB per Markdown document, 256 local references, eight
linked-document hops and 20 MiB per referenced file. The user's 2026-10-16
review called the reference, depth and size ceilings unnecessary next to the
1500-word soft limit and approved making them configurable with wider defaults
instead of removing them — the bounds stay as guards against mistakes, not as
writing policy.

Measurements on 2026-10-17: the largest panel document in the fleet is about
5 KiB and the largest reference count is 6; workspace data files (evidence logs,
data dumps) already reach 30-40 MiB, so the 20 MiB resource bound refused files
agents actually attach.

The 2026-09-17 config ruling requires behavior-shaping constants to be
configurable with a written reason for every limit. The bounds join the config
domains as `settings.to_user` (`base/config/domains/to_user.py`), cluster-pinned
(the gateway `.env` is the single copy every machine reads) with
`restart_required` "all" (the agent's home machine enforces the reads and the
gateway repeats the streaming guard).

## Decision

The four bounds become `settings.to_user` fields:

- `max_document_bytes`: 1 MiB (was 256 KiB). Guards against reading the wrong
  file as a document; the soft limit shapes authoring, and 1 MiB keeps even
  extreme reports readable.
- `max_references`: 1024 (was 256). Each reference costs one stat/version probe
  while reading; the bound caps a pathological document, not normal authoring.
- `max_chain_depth`: 16 (was 8). Real chains nest two or three documents; the
  bound stops runaway or circular recursion.
- `max_resource_bytes`: 64 MiB (was 20 MiB). Sized for evidence-grade
  attachments; the gateway buffers the body, so this is also its per-request
  memory bound.

The static `max_length=8` in the request schema disappears: the gateway rejects
oversized chains with 422 before dispatch, the ops listener re-checks against
the same configured value, and refusals name the configured number.

## Alternatives rejected

- Remove the bounds entirely (the review's first phrasing): an absent bound turns
  a wrong file reference into an unbounded read. The config ruling asks for
  reasons, not for the absence of every limit; widening with a written reason
  keeps the guard and answers the complaint.
- Keep the constants and only raise them: still hardcoded, still not operator
  adjustable, and against the 2026-09-17 ruling.
- Per-agent limits (the soft limit's shape): the reads happen in the home
  machine's ops listener and the gateway, not per-agent config consumers; a
  cluster-pinned value keeps behavior uniform with a single file.
- A `gateway`-capability domain: the consumption guard treats agent-runner
  cluster keys as invalid for gateway reads; "common" states the cluster-wide
  ownership these reads actually have.

## Consequences

Operators change the four values through the config surface (`MEIVA_TO_USER_MAX_*`
in the gateway `.env`); a change takes effect on the ops listeners and gateway
restart. The soft limit and its reminder are unchanged, and the process profiles
carry the new domain in all three kinds (the reading functions sit in the
agent-face closure). The 2026-10-15 decision's numeric bounds are superseded in
value; its behavior — bounded reads, a single resolver, source-machine files —
stands.
