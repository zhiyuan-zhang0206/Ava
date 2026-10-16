---
type: doc
title: Note timestamp policy
description: "The system_marker.show_timestamp split over NoteTag - ambient context and one-time guidance notes hide the ts; heartbeats, lifecycle transitions, task notifications, impersonation and NEW_SKILLS keep it."
tags:
- base
- library
- agent-history
---

# Note timestamp policy

Which system notes carry a wall-clock timestamp is one rendering decision of
[[base/agents/history/docs/timeline/timeline.ava.okf.md|timeline rendering]]; this
node owns that split.

```ava-contract
id = "note-timestamp-policy"
kind = "invariant"
statement = """system_marker.show_timestamp hides the ts for ambient context and the one-time guidance notes (compact reminder, SDK hint, agent reply, silent idle-continue, security, exec timeout, timezone, the to_user soft limit); heartbeats, lifecycle_* transitions, task notifications, impersonation and NEW_SKILLS keep it. NEW_SKILLS stays visible deliberately: the drift note is the only place that mid-window install is visible. The split is closed over NoteTag — a new member needs an explicit decision."""
code = ["base/agents/history/timeline.py:_NO_TIMESTAMP_NOTE_TAGS"]
tests = ["gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_system_note_show_timestamp_by_tag"]
```
