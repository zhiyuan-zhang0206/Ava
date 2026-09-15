---
type: doc
title: Exec Output — Soft Previews and Hard Limits
description: 'Line-count previews reduce long tool output without evicting full output still referenced by the current context; existing hard caps remain independent.'
tags:
- agent
- output
- config
---

# Exec output previews

`wrap_code_output` preserves the output envelope and timeout/cancel markers.
Before its hard character limit, `_exec_crop.py` can replace long multiline
output with its first and last lines plus a real recovery path. It does not
change tool execution, automatically rerun code, or call a model.

## Configuration

These `sandbox` settings are cluster-pinned and take effect on agent restart,
matching the existing exec limits. The corresponding environment aliases use
the `AVA_` prefix and uppercase field names.

| Field | Default | Meaning |
|---|---:|---|
| `exec_output_crop_after_lines` | 300 | Crop above this `splitlines()` count — one of three OR triggers; 0 disables soft cropping entirely |
| `exec_output_crop_after_chars` | 65536 | Crop above this character count (long lines); must be >= the hard inline limit |
| `exec_output_crop_after_bytes` | 65536 | Crop above this UTF-8 byte size (multibyte text); must be >= the hard inline limit |
| `exec_output_crop_head_lines` | 25 | Original leading lines to retain |
| `exec_output_crop_tail_lines` | 25 | Original trailing lines to retain |
| `exec_output_crop_archive_max_bytes` | 16 MiB | Per-agent UTF-8 byte budget for soft archives |

Three OR triggers select an output for preview: more than `exec_output_crop_after_lines`
lines, more than `exec_output_crop_after_chars` characters, or more than
`exec_output_crop_after_bytes` UTF-8 bytes; `exec_output_crop_after_lines = 0`
disables soft cropping entirely. The char and byte triggers are validated to sit
at or above the hard inline limit, so a preview never fires below what the hard
cap would have shown in full. Triggering is independent of the retained line
counts. Overlapping head and tail are never duplicated. The exact marker and
path count toward the preview size: if they make it no smaller, or it would
exceed the existing hard inline limit, soft cropping is skipped. Original line
endings and an unterminated last line are preserved. Outputs that are short, or
whose head and tail already cover every line, can therefore remain unchanged
even when a size trigger would select them. The fixed thresholds are offline
tuning choices, not an online percentile or a promise to crop an exact fraction
of future traffic.

## Recoverability and space

Full soft-cropped output is written before returning the preview, using an
exclusive UUID filename under the agent workspace: `.exec_output/crop_<uuid>.txt`.
The agent can read or grep that exact path. These files are independent of the
legacy `exec_*.txt` ring, so repeated hard overflows cannot remove a soft archive.

The native exec node passes its current messages explicitly to the formatter;
the parent does not read the exec child's `ava.state` slot. Archives referenced
by those messages (including reasoning and structured `execute_code` arguments), or by the
new output itself, are protected. Only unreferenced
soft archives are evicted, oldest first, to fit the byte budget. After compaction
drops a reference, that file is eligible for eviction; a summary that keeps its
path continues to protect it. This is a per-agent current-context guarantee,
not permanent retention of every historical or cross-agent reference.

If protected files plus the new output do not fit, soft cropping is skipped.
Storage failures likewise leave the output inline, subject to the hard limits,
and emit a diagnostic; no preview claims an archive that failed to write.
The 16 MiB budget is not an allocation, and the writer does not grow an unlimited
ring. Only serial native exec formatting writes this soft archive namespace.

## Existing hard limits

`exec_output_max_chars` remains 30,000 characters. On hard overflow,
`truncate_both_ends` keeps `max_chars // 2` characters at each end and archives
the text into `.exec_output/exec_*.txt`, keeping the latest 20 files. Old hard
archive references can expire; soft preview retention does not upgrade them.
The ava_code plugin's oversized context-file injection continues to use that
existing hard overflow behavior.

`exec_output_accumulation_max_chars` remains 1,000,000 characters and must be at
least the inline limit. While code runs, the accumulator keeps its first and
last halves and drops the middle. A `StreamCap` carries the true produced length
to the envelope, which names only a **surviving output** archive and states the
dropped middle is unrecoverable. Soft previews are disabled for such results;
they never call an already incomplete archive the full output.

## Dependencies

- [[agent/graph/tool-exec/tool-exec.ava.okf.md]] — native exec lifecycle and result dispatch
- `shared/config/sandbox.py` — validated configuration surface
- `tests/agent/test_exec_output_crop.py` — previews, real file recovery and bounded retention
