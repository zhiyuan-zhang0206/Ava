"""LLM streaming → SSE event fan-out: chat text + reasoning thinking + tool args code.

Stream chunk shapes (we always `bind_tools(...)` so
`coerce_content_to_string=False` — content is list-of-blocks, not string):

| chunk shape                                              | publish path              |
|----------------------------------------------------------|---------------------------|
| `content=[{"type":"text", "text":"..."}]`                | ChatStart / ChatDelta      |
| `content=[{"type":"thinking", "thinking":"..."}]`        | ReasoningStart / ReasoningDelta |
| `content=[{"type":"thinking", "signature":"..."}]`       | skip (signature_delta has no visible text) |
| `tool_call_chunks=[{"args":"...JSON frag..."}]`          | CodeStart / CodeDelta      |
| `content=""` or `[]` (message_start / usage frame)        | no-op                      |

Code uses `tool_call_chunks[].args` to accumulate JSON fragments, uses
`parse_partial_json` to extract the current value of the "code" field and
publish increments — `tool_call_chunks` is langchain's standardized args
view, present for every provider regardless of whether the tool call also
appears as a content block (anthropic `tool_use`) or only here (gemini /
openai). Providers differ on the chunk's `index`: anthropic sets the
`tool_use` content_block_index, gemini leaves it `None`. The code item's
block_idx is therefore computed locally (number of text/thinking content
blocks + the tool call's first-appearance ordinal), matching the committed
snapshot rule in `shared/timeline.py` rather than trusting the raw index.

`finish()` handles the rare case where partial JSON is only valid at the
last fragment, and publishes LLMDone — LLMDone is the frontend's timeline
reload trigger (after stream completes LangGraph state.messages is committed,
frontend fetches once to overwrite any partial items).

**Why not use LangChain `AsyncCallbackHandler`**: ChatAnthropic with tools
bound never triggers `on_llm_new_token` (`isinstance(content, str)` is
always False, see `langchain_anthropic/chat_models.py:1267`). Switched from
callback to `llm_node` calling `process_chunk` / `finish` inline in the
stream loop — control flow is explicit and does not depend on framework
internal callback timing.

Per-call instance: `llm_node` creates a new one on each entry (binds the
current agent_id + independent buf state).

Fan-out hands each event to the per-process `AgentEventPublisher` (`emit`, a
non-blocking enqueue), not an awaited Redis publish: these are best-effort
live-view events that must never stall the llm stream loop on a slow central
Redis. `process_chunk` / `finish` are therefore synchronous.
"""

import time
from typing import Any, cast

from langchain_core.messages import AIMessageChunk
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.utils.json import parse_partial_json

from shared.event_coalescer import DeltaCoalescer
from shared.event_publisher import AgentEventPublisher
from shared.live_events import (
    ChatDelta,
    ChatStart,
    CodeDelta,
    CodeStart,
    LLMDone,
    ReasoningDelta,
    ReasoningStart,
)
from shared.lm.reasoning import to_canonical_reasoning


class RedisStreamHandler:
    """Per-call streaming fan-out: chat text / reasoning / tool args → SSE events.

    Caller invokes `process_chunk(chunk)` per `AIMessageChunk` arriving from
    `astream`, then `finish()` once after the stream completes (success or
    cancel-with-partial). State (started flags, args buf, per-block published
    counts) is per-instance — reuse across LLM calls is unsupported.

    `msg_idx` is the position of this LLM call's produced AIMessage in
    state.messages (= `len(state.messages)` at llm_node entry). The streaming
    event `item_id = f"{msg_idx}.{block_idx}"` matches the id computed by the
    gateway timeline endpoint for the blocks inside the same AIMessage, so the
    frontend merge uses a stable key for direct matching.

    Events go to `AgentEventPublisher.emit` (non-blocking enqueue), so the
    methods are plain synchronous calls — no Redis await on the stream path.
    """

    def __init__(
        self,
        event_publisher: AgentEventPublisher,
        agent_id: int,
        msg_idx: int,
    ) -> None:
        self._publisher = event_publisher
        self._agent_id = agent_id
        self._msg_idx = msg_idx
        # *_started flags: per content_block_index, only emit *Start on the first
        # real delta, avoiding empty blocks (signature_delta only / boundary
        # chunks etc.) creating empty chat/reasoning items.
        self._chat_started: set[int] = set()
        self._reasoning_started: set[int] = set()
        self._code_started: set[int] = set()
        # Per reasoning block (keyed by content block_idx): monotonic ts of the
        # first and last thinking token seen for that block. Each block's
        # (last - first) delta is its own "thought for X seconds", persisted
        # per-block on the AIMessage by llm_node so a timeline reload shows the
        # real duration. Keying per block is model-agnostic: a provider that
        # interleaves thinking and text across several blocks gets one timer
        # per block, so the text streamed between two thinking blocks is never
        # folded into a thinking duration. Empty until a block streams a token.
        self._reasoning_first_ts: dict[int, float] = {}
        self._reasoning_last_ts: dict[int, float] = {}
        # Total characters of thinking content streamed this call — nonzero
        # only when the model produced reasoning. Used as a reasoning-token
        # fallback when the provider's usage_metadata does not break out
        # output_token_details (e.g. DeepSeek via anthropic-compat endpoint).
        self._total_reasoning_chars = 0
        # Distinct content-block indices seen for text/thinking blocks. Its
        # size is the offset where tool-call code items begin, matching the
        # committed snapshot rule in shared/timeline.py: a tool call always
        # follows all narration/reasoning (it terminates the turn), so by the
        # time a tool_call_chunk arrives this set is final.
        self._content_block_indices: set[int] = set()
        # tool_call grouping key -> resolved code block_idx. The key groups the
        # fragments of one tool call: anthropic streams args across many chunks
        # sharing an `index`; gemini sends one chunk per call with `index=None`
        # but a stable `id`. Ordinal = first-appearance order; block_idx offsets
        # past the content blocks so code never collides with text/thinking.
        self._tool_block_idx: dict[object, int] = {}
        # tool_call args JSON fragment accumulator — bucketed by the resolved
        # code block_idx, so multiple tool calls don't cross-pollute. Each buf
        # carries the already-published char count to compute the delta since
        # last publish.
        self._args_bufs: dict[int, str] = {}
        self._code_published: dict[int, int] = {}
        # Per code block (keyed by code block_idx): monotonic ts of the
        # first and last code fragment seen for that block. Drives the
        # per-block "wrote code for Xs" timing.
        self._code_first_ts: dict[int, float] = {}
        self._code_last_ts: dict[int, float] = {}
        # Delta coalescer: LLM streaming emits one delta per token fragment
        # (50-100/s); buffered per item and flushed ONE event per item per
        # SSE event window (40ms), so the producer rate matches the frontend
        # render window. Start / Done events bypass it (the frontend needs
        # them immediately). finish() flushes the remainder; flush_deltas()
        # is the explicit drain for callers / tests.
        self._coalescer = DeltaCoalescer(self._emit_delta)
        # Whole-call wall-clock (ms) from request start to stream completion,
        # stamped by `agent.graph._llm._stream_with_cache_retry` after the
        # call succeeds (retries included). Consumed by
        # `_finalize_turn_observability` → `log_llm_usage(latency_ms=...)` so
        # the llm_usage agent_event carries per-call latency for the ops
        # monitor panel. None until a call completes; reset() deliberately
        # does NOT clear it (a stale-cache retry is one logical call).
        self.llm_latency_ms: float | None = None
        # Decode-stage wall-clock: last-chunk arrival minus first-chunk arrival
        # (monotonic, measured in `_llm._consume_stream_with_stall_timeout`),
        # i.e. pure generation time excluding network / queue / prefill — the
        # denominator of the ops panel "spawn-stage output TPS". None until a
        # call completes; stays None for non-streaming fallback calls and
        # empty streams (no honest window → NULL in the payload, never a fake
        # number). Stamped after the LAST successful attempt; reset()
        # deliberately does NOT clear it (same one-logical-call rule as
        # llm_latency_ms — the stale-cache retry re-stamps it fresh).
        self.llm_decode_ms: float | None = None

    def _block_id(self, block_idx: int) -> str:
        """`f"{msg_idx}.{block_idx}"` — same rule the gateway uses when computing
        the commit version; the snapshot and the streaming item share the same id
        for merge matching."""
        return f"{self._msg_idx}.{block_idx}"

    def process_chunk(self, chunk: AIMessageChunk) -> None:
        """Dispatch one streaming chunk to the SSE fan-out.

        text / reasoning / code judged independently along three paths: when
        the same chunk carries reasoning + tool_use simultaneously (content
        block boundary chunk) or multi-block cases, all three paths fire
        without exclusion.

        Reasoning may arrive via canonical `thinking` content blocks
        (ReasoningContentChatModel / Anthropic / Gemini) or via
        `additional_kwargs["reasoning_content"]` (ChatMoonshot / ChatXAI).
        Both paths are handled — the `additional_kwargs` variant uses
        block_idx=0 since the reasoning is not embedded in content blocks.
        """
        self._process_content(chunk.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        self._process_additional_reasoning(chunk.additional_kwargs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # tool_call_chunks is list[ToolCallChunk] TypedDict (not list[dict]),
        # list type covariance makes the helper-receive type mismatch — inline
        # handle to avoid a type cast
        for tcc in chunk.tool_call_chunks or []:
            block_idx = self._tool_block_idx_for(tcc)
            args_frag = tcc.get("args") or ""
            if not args_frag:
                continue
            self._args_bufs[block_idx] = self._args_bufs.get(block_idx, "") + args_frag
            self._publish_code_increment(block_idx)

    def _process_additional_reasoning(self, additional_kwargs: dict[str, Any]) -> None:
        """Extract reasoning from `additional_kwargs["reasoning_content"]`.

        Community packages (ChatMoonshot / ChatXAI) carry reasoning in
        `additional_kwargs` rather than canonical content blocks. Each chunk
        carries the raw delta fragment (not accumulated), so the fragment is
        published directly as a reasoning delta at block_idx=0 — the same
        block_idx used by the timeline for additional_kwargs reasoning.

        Empty / missing / non-string values are no-ops.
        """
        reasoning = (additional_kwargs or {}).get("reasoning_content")
        if not reasoning or not isinstance(reasoning, str):
            return
        self._content_block_indices.add(0)
        self._publish_reasoning_delta(0, reasoning)

    def _tool_block_idx_for(self, tcc: ToolCallChunk) -> int:
        """Resolve the code block_idx for one tool_call_chunk, grouping the
        chunk fragments of the same tool call and offsetting past content blocks.

        Grouping key is the chunk's `index` when set (anthropic shares it across
        the call's arg-delta chunks), else its `id` (gemini emits one chunk per
        call with `index=None`). The first chunk of each distinct call claims the
        next code slot: `len(content blocks seen) + <calls seen so far>`. Cached
        so later chunks of the same call resolve to the same block_idx.
        """
        raw_index = tcc.get("index")
        key: object = raw_index if raw_index is not None else tcc.get("id")
        if key not in self._tool_block_idx:
            self._tool_block_idx[key] = len(self._content_block_indices) + len(self._tool_block_idx)
        return self._tool_block_idx[key]

    def finish(self) -> None:
        """Flush remaining code increment + publish LLMDone (frontend reload trigger).

        Rare case where partial JSON is only valid at the last fragment of the
        stream — flush guarantees the frontend's real-time view gets the full
        code. LLMDone makes the frontend re-fetch timeline to pull LangGraph
        state.messages' committed version, overwriting any items marked partial.
        """
        for block_idx in list(self._args_bufs.keys()):
            self._publish_code_increment(block_idx)
        self._coalescer.flush()
        self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())

    def reset(self) -> None:
        """Drop all per-stream state so the handler can re-stream the same
        message from scratch (the stale-cache retry path in
        `_llm._stream_with_cache_retry`).

        Reuse without reset duplicates streamed content: `*_started` sets
        suppress the Start events while deltas re-append (doubled partial
        text), `_args_bufs` concatenates the second attempt's fragments onto
        the first's (parse_partial_json then fails → code delta stalls), and
        the timing/token accumulators double-count. Only the publisher /
        agent_id / msg_idx (the message's absolute position — unchanged by a
        retry) survive."""
        self._chat_started.clear()
        self._reasoning_started.clear()
        self._code_started.clear()
        self._reasoning_first_ts.clear()
        self._reasoning_last_ts.clear()
        self._total_reasoning_chars = 0
        self._content_block_indices.clear()
        self._tool_block_idx.clear()
        self._args_bufs.clear()
        self._code_published.clear()
        self._code_first_ts.clear()
        self._code_last_ts.clear()
        self._coalescer.flush()

    def flush_deltas(self) -> None:
        """Drain buffered deltas to the publisher now (one event per item,
        concatenated). Called by finish(); exposed for tests and for callers
        that need the live view caught up without ending the stream."""
        self._coalescer.flush()

    def _emit_delta(self, key: str, content: str) -> None:
        """Coalescer flush callback: reconstruct the typed delta event from
        the buffered key (kind|item_id) and hand it to the publisher."""
        kind, item_id = key.split("|", 1)
        if kind == "chat":
            self._publisher.emit(
                ChatDelta(
                    agent_id=self._agent_id, item_id=item_id, content=content
                ).model_dump_json()
            )
        elif kind == "reasoning":
            self._publisher.emit(
                ReasoningDelta(
                    agent_id=self._agent_id, item_id=item_id, content=content
                ).model_dump_json()
            )
        else:
            self._publisher.emit(
                CodeDelta(
                    agent_id=self._agent_id, item_id=item_id, content=content
                ).model_dump_json()
            )

    def _process_content(self, content: str | list[Any]) -> None:
        """Process chunk.content: string (legacy / test mock, block_idx=0) or
        list-of-blocks (production). Records each text/thinking block's index in
        `_content_block_indices` so the tool-call code offset matches the
        committed snapshot.

        Provider-native reasoning blocks are first folded to the canonical
        `thinking` shape (`shared.lm.reasoning`), so this method only ever sees
        text / thinking — it never branches on provider."""
        content = to_canonical_reasoning(content)
        if isinstance(content, str):
            if content:
                self._content_block_indices.add(0)
                self._publish_chat_delta(0, content)
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            d = cast(dict[str, Any], block)
            block_idx = d.get("index", 0) or 0
            btype = d.get("type")
            if btype == "text":
                self._content_block_indices.add(block_idx)
                text = d.get("text") or ""
                if text:
                    self._publish_chat_delta(block_idx, text)
            elif btype == "thinking":
                # signature_delta also produces type=thinking but only carries
                # the "signature" field, no "thinking" — signature is an opaque
                # verifier issued by the server, client transparently echoes,
                # not user-visible text, skip publishing. Still counts toward the
                # content-block offset (it shares its thinking block's index, so
                # the set dedupes).
                self._content_block_indices.add(block_idx)
                thinking = d.get("thinking") or ""
                if thinking:
                    self._publish_reasoning_delta(block_idx, thinking)

    def _publish_chat_delta(self, block_idx: int, frag: str) -> None:
        item_id = self._block_id(block_idx)
        if block_idx not in self._chat_started:
            self._publisher.emit(
                ChatStart(agent_id=self._agent_id, item_id=item_id).model_dump_json()
            )
            self._chat_started.add(block_idx)
        self._coalescer.append(f"chat|{item_id}", frag)

    @property
    def reasoning_ms_by_block(self) -> dict[int, int]:
        """Per-block thinking wall-clock in milliseconds, keyed by block_idx.

        Each entry = (last thinking token - first thinking token) for that
        block. Empty when this call produced no thinking. A turn that
        interleaves several thinking blocks yields one entry per block, so the
        text streamed between them is never folded into a thinking duration —
        the measurement does not assume thinking precedes all text. The tail
        gap between a block's last thinking token and the next block's start is
        negligible, so each value is the user-facing "thought for X seconds"
        for that block.
        """
        return {
            block_idx: int((self._reasoning_last_ts[block_idx] - first) * 1000)
            for block_idx, first in self._reasoning_first_ts.items()
        }

    @property
    def code_ms_by_block(self) -> dict[int, int]:
        """Per-block code wall-clock in milliseconds, keyed by code block_idx.

        Each entry = (last code fragment - first code fragment) for that
        block. Empty when this call produced no tool calls. A turn with several
        tool calls yields one entry per block, so the narration streamed
        between them is never folded into a code duration. The tail gap between
        a block's last fragment and the next block's start is negligible, so
        each value is the user-facing "wrote code for X seconds" for that block.
        """
        return {
            block_idx: int((self._code_last_ts[block_idx] - first) * 1000)
            for block_idx, first in self._code_first_ts.items()
        }

    @property
    def total_reasoning_chars(self) -> int:
        """Total characters of thinking content streamed this LLM call.

        Accumulated from every ReasoningDelta fragment. Zero when the model
        produced no reasoning. Used as a fallback to estimate reasoning
        tokens when usage_metadata.output_token_details is absent.
        """
        return self._total_reasoning_chars

    def _publish_reasoning_delta(self, block_idx: int, frag: str) -> None:
        now = time.monotonic()
        if block_idx not in self._reasoning_first_ts:
            self._reasoning_first_ts[block_idx] = now
        self._reasoning_last_ts[block_idx] = now
        self._total_reasoning_chars += len(frag)
        item_id = self._block_id(block_idx)
        if block_idx not in self._reasoning_started:
            self._publisher.emit(
                ReasoningStart(agent_id=self._agent_id, item_id=item_id).model_dump_json()
            )
            self._reasoning_started.add(block_idx)
        self._coalescer.append(f"reasoning|{item_id}", frag)

    def _publish_code_increment(self, block_idx: int) -> None:
        """Extract the current value of the "code" field from the specified
        block's accumulated args buf, publish the delta since the last publish.

        parse_partial_json failure / haven't reached "code" key yet / value not
        a string all no-op — try again on the next fragment. Zero-length
        increment (value unchanged) also no-op.
        """
        buf = self._args_bufs.get(block_idx, "")
        if not buf:
            return
        try:
            partial = parse_partial_json(buf)
        except Exception:
            return
        if not isinstance(partial, dict):
            return
        code_so_far = cast(dict[str, Any], partial).get("code")
        if not isinstance(code_so_far, str):
            return
        published = self._code_published.get(block_idx, 0)
        delta = code_so_far[published:]
        if not delta:
            return
        now = time.monotonic()
        if block_idx not in self._code_first_ts:
            self._code_first_ts[block_idx] = now
        self._code_last_ts[block_idx] = now
        item_id = self._block_id(block_idx)
        if block_idx not in self._code_started:
            self._publisher.emit(
                CodeStart(agent_id=self._agent_id, item_id=item_id).model_dump_json()
            )
            self._code_started.add(block_idx)
        self._coalescer.append(f"code|{item_id}", delta)
        self._code_published[block_idx] = len(code_so_far)
