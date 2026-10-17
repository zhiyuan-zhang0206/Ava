---
type: doc
title: System markers
description: "The system-marker dispatch in components/timeline/markers.tsx — card families, the fail-loud unrecognized alarm and the ephemeral payload markers."
tags:
- frontend
---

# System markers

`components/timeline/markers.tsx` classifies a `system_marker` item by its
`source` (a backend `NoteTag`) into three card-rendered families — lifecycle
transitions, memory recall, guidance notes — plus the bare-rendered ephemeral
catch-all. Card visuals resolve from the timeline-colors slot chosen in Display
settings and are composed at call time, so one family swap restyles every
marker; titles are i18n keys under the `markers` namespace with an English
fallback.

```meiva-contract
id = "marker-dispatch-sets"
kind = "invariant"
statement = """The three exported dispatch sets are the renderer's exact dispatch table: the backend NoteTag value set is asserted (backend contract test) to be a subset of LIFECYCLE_TAGS ∪ MEMORY_SOURCES ∪ NOTE_SOURCES, and a frontend test iterating the same sets locks every member to its chip and never the red alarm. A backend tag without a branch here falls to the ephemeral path and surfaces as the fail-loud UnknownMarkerChip, not a silent fallback."""
code = ["ui/web/src/components/timeline/markers.tsx:LIFECYCLE_TAGS", "ui/web/src/components/timeline/markers.tsx:MEMORY_SOURCES", "ui/web/src/components/timeline/markers.tsx:NOTE_SOURCES"]
tests = ["tests/contracts/test_lint_marker_contract.py::test_note_tag_and_frontend_dispatch_sets_agree", "ui/web/src/components/timeline/timeline.test.tsx::Marker contract: every dispatch-set source renders without the red alarm::positive control: an unknown source still renders the red alarm", "tests/e2e/flow/test_message_flow.py::test_message_flow_renders_full_turn_without_unrecognized_marker", "tests/e2e/flow/test_compact_flow.py::test_force_compact_renders_envelope_without_unrecognized_marker"]
```

The payload-identified ephemeral markers render bare: `compact_done` and
`compact_request:*` are normally filtered at the SSE layer (`applySystemEvent`),
so their branches only catch stale rows; `cancelled` is silent (the stop button
has no UI noise); `error:*` renders the error chip; anything else renders the
red `UnknownMarkerChip` with the raw `source`/`payload` shown so the missing
case is obvious. The alarm also logs to the dev console — the timeline is
client-only `"use client"` code, so no `window` guard is needed.

```meiva-contract
id = "ephemeral-marker-testids"
kind = "invariant"
statement = """EphemeralSystemMarker is the bare render path: compact_done / compact_request / cancelled render nothing, error:* renders the error chip, and any other payload renders the red unrecognized alarm. Its data-testid values are stable assertion hooks: marker-unrecognized must never render when the frontend is adapted and marker-error is the legit error path; absence of the alarm node in an e2e run means the #1017 alarm class did not render."""
code = ["ui/web/src/components/timeline/markers.tsx:EphemeralSystemMarker", "ui/web/src/components/timeline/markers.tsx:UnknownMarkerChip"]
tests = ["ui/web/src/components/timeline/markers.test.tsx::marker data-testid hooks (Task #1018)::unrecognized alarm renders data-testid=marker-unrecognized", "ui/web/src/components/timeline/markers.test.tsx::marker data-testid hooks (Task #1018)::error marker renders data-testid=marker-error", "ui/web/src/components/timeline/markers.test.tsx::marker data-testid hooks (Task #1018)::filtered payloads (compact_done / compact_request / cancelled) render neither", "tests/e2e/flow/test_error_recovery.py::test_llm_error_renders_error_marker_and_agent_recovers"]
```

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the item list that hosts system markers.
- [[base/agents/messages/docs/kwargs.meiva.okf.md|Message Metadata Contract]] — the backend NoteTag closed set the dispatch sets mirror.
