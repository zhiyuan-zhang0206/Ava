# Unified `ava.understand` multimodal primitive

## Context

The agent is CodeAct: its one tool is `execute_code`, and capabilities are
Python functions under `ava`. The move "have an LLM look at some material and
answer a question" was hard-coded in two narrow spots — a vision helper
(image/video) and `web.fetch`'s internal page-answer step — with no way to apply
it to an arbitrary blob: a long text the agent already holds, a downloaded PDF,
an audio file.

The capability ladder had a missing middle rung:

| Rung | Cost |
|---|---|
| Agent reads material into its own context | context tokens — a 50K-char page bloats the main thread, can trigger compaction |
| `spawn` a full sub-agent | seconds + a process — a sledgehammer for a one-shot summarize |
| **the gap** | — |

The load-bearing motive is **context isolation**: a separate LLM call turns 50K
chars into a 500-char answer, and the main agent only ever sees the answer (the
same shape as a web-search summarizer). The API-key worry that argues against
"let the agent write its own script calling an LLM API" also dissolves — the SDK
reads the key from settings; the agent's CodeAct never touches a key.

## Decision

Add `ava.understand(input, prompt) -> str`, a single top-level SDK function that
answers a prompt over almost any material — literal text, text files, images,
video, audio, PDF. It replaces the narrower vision helper (deleted) and becomes
the answer engine that `web.fetch` delegates to.

- **Top-level callable**, not `ava.understand.understand()`. Implementation
  lives in a private `_understand.py` so the *function* `ava.understand` and a
  same-named *submodule* don't ambiguously bind.
- **`UnderstandError` hangs off the function** (`ava.understand.UnderstandError`),
  since a top-level function has no namespace to host its exception. Named in the
  docstring's Raises section; the catch form is discoverable via `help(ava)`.
- **One `input` arg disambiguated by `Path(input).is_file()`**: an existing file
  is read, otherwise the string *is* the material.
- **Routing by suffix**: known media suffixes (image/video/audio/PDF) are read as
  bytes and sent as a media part; every other existing file (`.txt/.md/.csv/.json`,
  source, extensionless) is read as UTF-8 and sent as text, same path as literal
  text. An undecodable binary with an unrecognized suffix raises a legible error
  naming the suffix.
- **Modality split is structural; model IDs are config**. Text → a chat-model
  builder; media → the Gemini client directly (media parts need that path; the
  builder's gemini branch is text-shaped). Which model each side uses is a
  per-cluster / per-agent setting with sensible defaults.
- **`web.fetch` delegates its answer step** to `understand(page, prompt)`. The
  framed page is text, so it takes the text path. `UnderstandError` is wrapped
  back into `FetchError` to preserve `web`'s exception contract.
- **Media quality knobs are config, defaults not maxed**: resolution defaults to
  `high` (see detail), thinking defaults to medium (don't over-think) — because
  `understand` is high-frequency and high resolution already multiplies input
  tokens per image/frame. The text path inherits the global reasoning effort with
  no understand-specific override.

## Alternatives rejected

- **Explicit `path=` / `text=` params** instead of one disambiguated `input` —
  defeats the "one input" ergonomics. The only failure mode of `is_file()`
  disambiguation is a text snippet that exactly equals a real filename, which is
  vanishingly rare since material is usually far longer than a path.
- **Text-only, caller pre-reads files** — would drop binary media entirely,
  defeating the multimodal goal.
- **`ava.understand.understand()` submodule form** — the flatter call site was
  chosen deliberately; the submodule form also broke `import ava.understand as
  mod` against the function binding.
- **A single multimodal model with high thinking for everything** — revised to a
  modality split: the text provider has no vision and is strong/cheap on text;
  the media provider natively decodes media bytes. Cheaper and better-fit per side.
- **Hard-coding the model IDs** — moved to settings so each side is tunable per
  cluster / per agent; only the modality split stays structural.
- **Maxing every media knob** — rejected for a high-frequency primitive: high
  resolution already multiplies token cost, so thinking is held at medium.

## Consequences

- One primitive now owns the "material + prompt → answer" move across all
  modalities; the vision helper and the fetch-internal answerer collapse into it.
- **Behavior change in `web.fetch`**: the page answer used to run on the agent's
  own model (thinking off); it now runs on the fixed text-path model at its
  default thinking. The `web` exception contract is preserved by re-wrapping.
- A new context-isolation tier exists between "read into context" and "spawn a
  sub-agent" — cheap, zero context pollution.
- The exception lives on a function attribute rather than in a module namespace —
  an accepted ergonomic cost of the flat call site, mitigated by docstring and
  `help(ava)` discoverability.
- The modality split is locked structurally (which client handles which kind),
  while the specific models and media-quality knobs stay tunable, so model
  upgrades don't require touching the routing logic.
