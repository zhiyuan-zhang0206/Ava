# Agent view: the context rows are per block, and Added context is the Messages row's height

Decision (2026-10-09, later that day): the Context size row draws one bar per block
(thinking, text, call, output, inbound, note), not per message, and the Added context
row is gone. A block's own tokens set its height in the Messages row (square root,
bottom-aligned, a minimum so small blocks show; "equal" is a setting). The Context size
row is a setting too (on by default). Estimated counts read "~190 tokens", exact ones
carry no prefix.

Why: a bar per message united the blocks of an AIMessage (thinking, text, call) into one
bar, so the Added row did not match the Messages row above it, and a tool call hid inside
its thinking bar next to the output. The rows must hold the same blocks at the same x and
width. The data is the unit itself: `context_total` (the context through the block),
`session` and `request` ride on `RunTimelineUnit`, so there is no second list to keep
aligned.

Mechanics: the blocks of one AIMessage each add their share of the message (the same
estimator split their `context_tokens` use) to the context before it; the first block
therefore starts from the request's `input_tokens` and the last ends at the context
through the whole message. A block and its bar are one selectable thing in two rows.

Rejected: keeping per-message bars and splitting them only in the Messages row (two
units for one thing); an Added row of per-block bars (the same height information, a
row further from the block it describes).

Supersedes the per-message rows of `2026-10-09-agent-view.md` (the message was the unit there).
