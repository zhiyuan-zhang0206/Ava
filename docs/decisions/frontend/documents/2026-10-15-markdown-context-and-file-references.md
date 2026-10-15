# Markdown shared context with source-machine file references

## Context

Scope: the To user reading panel must share Ava's typography, theme and link
behavior while displaying agent-authored reports, images and downloadable files.
Agents can work in repositories or arbitrary directories. Requiring an assets
folder adds copying and stale duplicates. The existing document is a live
workspace file, not a published or archival object.

Prior art: the existing ChatMarkdown/react-markdown surface controls React
components and URLs; markdown-it-py parses backend document references; the
cluster's authenticated ops listener already owns home-machine reads. The
existing bounded binary daemon response is sufficient for report attachments.

Constraints: source-machine path interpretation and authenticated access are
required. Keeping the live-file/no-publication model and bounding attachments to
20 MiB are deliberate first-version limits. A dedicated attachment folder and
per-agent HTTP server are unnecessary. No additional background worker, registry,
database content store or external URL crawler is introduced.

## Decision

Read only `to_user.md` and render it through the shared Markdown component,
without raw HTML. Remove the format setting, HTML response and frame navigation.
Keep existing HTML files untouched; report a missing Markdown replacement and
instruct agents to migrate their shared context.

Parse local links and images into resource IDs. Resolve relative paths against
the containing document, absolute and home-relative paths on the owning runner.
Nested Markdown remains a document with its own path base. On every request,
walk the requested reference-ID chain from the current root document; arbitrary
browser-supplied filesystem paths are never accepted. OS permissions still apply.

Use the existing ops listener for bounded binary reads, and the gateway for
browser authentication and safe serve-back headers. Resource reads share ops
concurrency, worker and maintenance ownership. Markdown is limited to 256 KiB,
256 local references and eight reference hops; each binary resource is limited
to 20 MiB at both ends. Only requested resources are read; there is no eager
recursive packaging. The current response buffers those bounded bytes.

## Consequences

Fonts, sizes and colors follow the application, and local files work on remote
browsers without an agent starting a server. Removing a link revokes subsequent
reads. Files remain dependent on their source machine and filesystem lifetime.
The shallow document watcher stays bounded; explicit refresh/focus rechecks
attachment stamps and refreshes same-name images. Large media, range requests,
archival history and offline access are outside this change.

The document RPC schema changes, so a future rollout must coordinate gateway,
runner and frontend versions. An old runner rejects the new reference field,
and an old document response includes the removed format field. Merging this
change does not deploy it. Existing HTML files and stored format pins remain
on disk/in storage; the new code ignores retired pins and prompts agents to
write Markdown when they next run.

## Alternatives considered

- A dedicated assets folder simplifies directory serving but makes agents copy
  and reorganize files. The user explicitly prefers ordinary file references.
- Publishing a snapshot with copied attachments preserves files through cleanup
  and machine outages, but introduces durable storage and retention semantics
  that the current live-context requirement does not need.
- Exposing a generic filesystem path endpoint avoids parsing, but expands the
  browser surface beyond files the document actually presents.
- Keeping an HTML iframe retains arbitrary styling and navigation, which is the
  source of the inconsistent reading experience this change removes.
