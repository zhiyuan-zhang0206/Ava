"""Node-text generation — the LLM pass that materializes sealed nodes.

One model call per node: the node's input material (a leaf's rendered blocks, or
an upper node's children texts in stream order) plus a prompt carrying the
node's character ask. The generated text must fit the node's narrative budget
(<= 1/10 of the direct input tokens — the frozen contract in `seal.py`); an
over-budget response gets bounded compression retries, and a node still over
budget after them fails with an error result rather than writing an
over-budget text (acceptance anchor: zero over-budget texts in storage).

Determinism boundary: the partition (block fold + seal cascade) is fully
deterministic, the generated TEXT is not (an LLM call). Same input re-run =
same tree structure; the text is content, the structure is the contract.

Nodes generate in parallel with a bounded fan-out, isolated per node: one
node's provider failure never sinks the batch — it is recorded as that node's
error and retried on a later pass. This mirrors the pilot's bisect-to-singles
behavior (`ava.understand` failed whole-batch, so the pilot split batches on
failure to isolate the cause) with the isolation built in.

Request shape (task #4674): when the caller supplies a request prefix and the
agent's tool schema, the call is agent-shaped — the agent's own leading
messages (byte-identical, so the provider serves the prefix from its cache)
plus one trailing message carrying the node material, the prompt, and a
text-only clause; a tool-call response is answered with a ToolMessage error
and re-invoked up to `GenParams.tool_rounds` rounds. Bare requests (no
prefix/tools) keep the material-only shape.

Calibration provenance for every constant here: the v0.3 demo's machine check
(task #3704; 72 nodes, 0 over budget across 3 levels).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

from loguru import logger

from shared.agents.history.hierarchy import ENGINE_VERSION, PROMPT_VERSION
from shared.agents.history.hierarchy.seal import narrative_budget_tok
from shared.agents.history.hierarchy.tokens import count_tokens
from shared.lm._call import extract_text, invoke_response, invoke_text

# The node kinds `build_prompt` serves: a leaf's input is rendered source
# blocks, an upper node's input is its children's texts.
_LEAF = "leaf"
_NODE = "node"

# Escaped "Zhaiyao:" (the Chinese label for "summary") — repo rule: no raw CJK
# in code (labeler precedent). Some models prefix their answer with it.
_SUMMARY_PREFIXES = ("\u6458\u8981\uff1a", "\u6458\u8981:")

_LEAF_PROMPT = """You are the summarizer of a cluster understanding layer. \
The input is one stretch of an agent's work log: a block is one \
"think -> act -> observe" cycle or one inbound message; i### is the message \
index; [thinking] marks the model's reasoning; tool calls follow the code that \
issued them; …[omitted N chars]… marks an elided passage.

Write the summary of this stretch in Chinese:
- lead with one sentence: what the stretch was doing and where it ended up;
- then the key actions and their results, the friction met (errors, rework, \
environment pitfalls, misunderstandings) and unclosed threads — woven in \
naturally, no fixed fields;
- prefer concrete facts: copy task/PR numbers, file names and numbers \
verbatim; stay neutral and factual;
- length: aim for {lo}-{hi} characters, {hi} is a HARD LIMIT — exceeding it \
fails the bar (when content is dense, keep identifiers, results, friction and \
threads, drop background);
- the text is shown to a person as-is and also becomes the input of the \
next-level summary — make it serve both;
- output only the summary body: no title, no prefix, no suffix."""

_NODE_PROMPT = """You are the summarizer of a cluster understanding layer. \
The input is a sequence of lower-level node summaries in chronological order. \
Reduce them into one more abstract summary of the period.

Write the summary in Chinese:
- lead with one sentence: what the period was about as a whole and where it \
ended up;
- then the key threads and where they moved, recurring friction, and unclosed \
threads — the higher the level, the more the text reads as patterns and \
direction rather than single details;
- keep the essential identifiers (task/PR numbers, file names, numbers) \
verbatim; stay neutral and factual;
- length: aim for {lo}-{hi} characters, {hi} is a HARD LIMIT — exceeding it \
fails the bar (when content is dense, keep identifiers, results, friction and \
threads, drop background);
- the text is shown to a person as-is and also becomes the input of the \
next-level summary — make it serve both;
- output only the summary body: no title, no prefix, no suffix."""

_COMPRESS_PROMPT = """The summary below exceeds its character limit. Rewrite \
it within {n} characters ({n} is a HARD LIMIT — exceeding it fails the bar). \
Keep every identifier (task/PR numbers, file names, numbers, key results) and \
every friction and open thread; cut background and repetition. Write in \
Chinese. Output only the rewritten body: no title, no prefix, no suffix."""

_PROMPTS = {_LEAF: _LEAF_PROMPT, _NODE: _NODE_PROMPT}


class GenerateError(Exception):
    """A generation-pass failure: provider call failed after retries, or the
    text could not be fitted into the node's budget."""


@dataclass(frozen=True)
class GenParams:
    """Generation calibration (defaults = the v0.3 contract, task #3704).

    These are engine-calibration constants with written reasons, the same
    shape as `seal.SealParams`; which model a run uses is resolved per target
    agent (`shared.agent_snapshot.agent_effective_model` — overlay preferred,
    fleet default else), with `settings.lm.hierarchy_model` as the last-resort
    fallback.
    """

    # Ask under the budget so an ordinary response lands inside the hard
    # check; the demo measured outputs at ~1/13 of source, comfortably in.
    ask_ratio: float = 0.85
    # The ask is in characters, the budget in tokens; CJK runs ~0.7-0.8
    # tokens/char in o200k, so the character ask must over-ask the token cap.
    chars_per_token: float = 1.4
    # Floor for tiny inputs: below this the ask is unreadably small and the
    # summary becomes a fragment.
    min_ask_chars: int = 300
    # The lower bound of the prompt's "aim lo-hi" range, as a fraction of hi:
    # a range steers length better than a single number.
    lo_ratio: float = 0.6
    # Bounded compression retries for an over-budget response (each retry is
    # one extra cheap-model call); still over budget after these = node fails.
    compress_attempts: int = 2
    # Second compression attempt asks a tighter target (pilot calibration).
    compress_retry_scale: float = 0.85
    # Bounded tool-call refusal rounds for the agent-shaped request (task
    # #4674): the request carries the real tool schema, so a response may call
    # the tool instead of writing text; each such response is refused with an
    # error result and re-invoked, and exhausting the rounds fails the node
    # (retried on a later pass) — never an unbounded loop.
    tool_rounds: int = 3
    # The demo's effective reasoning level (its "low" clamps onto "high" for
    # deepseek); the deepseek registry default ("max") is the agent-brain
    # level and far more than a summarizer needs.
    reasoning_effort: str = "high"


# One batch's parallel fan-out. Mirrors the SDK batch ceiling
# (`ava/_batch.DEFAULT_BATCH_MAX_CONCURRENT` — "must not turn one action into
# an account-wide request burst"); callers may raise or lower it per capacity.
DEFAULT_MAX_CONCURRENT = 12

# Transient provider failures (rate limit / 5xx / connection) retried once
# inside `invoke_text`; matches `settings.lm.llm_invoke_retry_attempts`'s
# default for the SDK invoke paths.
DEFAULT_RETRY_ATTEMPTS = 1

# The clause appended at the tail of the agent-shaped request (task #4674).
# The request carries the real tool schema for cache parity with the agent's
# own calls, so the instruction must forbid the tool call explicitly — the
# same framing as the compaction instruction ("anything other than text is
# discarded"). English per the repo rule (no raw CJK in code); the summary
# itself is Chinese because the prompts ask for it.
_TEXT_ONLY_CLAUSE = (
    "Reply with plain text only — do not call any tool; anything other than text is discarded."
)

# The refusal handed back when a response does carry tool calls: tools cannot
# run on this call, and the only correct continuation is the text answer.
_TOOL_REFUSAL = (
    "Error: tools are unavailable in this call. Retry by writing the requested "
    "summary as plain text — no tool calls."
)


@dataclass(frozen=True)
class GenRequest:
    """One node to generate: stable id, kind, material, and the request prefix.

    `prefix` (task #4674) is the agent's own leading messages up to the node's
    span start — SystemMessage snapshot plus prior conversation, byte-identical
    to what the agent sends — so the generation request rides the provider's
    prefix cache; empty means the material-only request (defensive fallback
    for histories without a SystemMessage head).
    """

    nid: str
    kind: str  # leaf | node
    input_text: str
    prefix: tuple[Any, ...] = ()


@dataclass(frozen=True)
class GenResult:
    """One node's outcome; `error` set = nothing is written for this node.

    `src_tok` / `budget_tok` are the measured input size and the applied
    budget — kept on the result for the caller's logs and acceptance checks.
    """

    nid: str
    src_tok: int
    budget_tok: int
    out_tok: int | None = None
    text: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether this node produced text (no error)."""
        return self.error is None and self.text is not None


def char_bounds(src_tok: int, params: GenParams | None = None) -> tuple[int, int]:
    """The prompt's character ask ``(lo, hi)`` for an input of `src_tok` tokens."""
    p = params or GenParams()
    ask_tok = int(narrative_budget_tok(src_tok) * p.ask_ratio)
    hi = max(int(ask_tok * p.chars_per_token), p.min_ask_chars)
    return int(hi * p.lo_ratio), hi


def build_prompt(kind: str, src_tok: int, params: GenParams | None = None) -> str:
    """The generation prompt for one node of `kind` with a `src_tok` input."""
    template = _PROMPTS.get(kind)
    if template is None:
        raise ValueError(f"unknown node kind {kind!r} — expected {'/'.join(_PROMPTS)}")
    lo, hi = char_bounds(src_tok, params)
    return template.replace("{lo}", str(lo)).replace("{hi}", str(hi))


def clean_text(text: str) -> str:
    """Strip whitespace and a leading "summary:" label from a model response."""
    stripped = text.strip()
    for prefix in _SUMMARY_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
    return stripped


def input_hash(kind: str, input_text: str) -> str:
    """The generation cache key for one node: identical input, identical key.

    Covers the engine and prompt versions, so bumping either invalidates every
    cached text — a template change must be re-generated, never silently
    reused. The storage layer keys its reuse cache on this value.
    """
    digest = hashlib.sha256()
    digest.update(f"{ENGINE_VERSION}|{PROMPT_VERSION}|{kind}|".encode())
    digest.update(input_text.encode())
    return digest.hexdigest()


def text_hash(text: str) -> str:
    """The content hash of a generated node text (idempotent-write key)."""
    return hashlib.sha256(text.encode()).hexdigest()


def build_generation_llm(model: str, params: GenParams | None = None) -> Any:
    """Build the generation chat model at the pass's reasoning effort.

    The single source of the generation model's construction: `generate_nodes`
    builds one through here when the caller supplied none, and the hierarchy
    callers (the worker's job child, the manual build script) build their
    one-per-run model through here too — so every path reaches the same
    provider shape and effort (`GenParams().reasoning_effort`, unless the
    caller passes params). The caller owns the returned model: close it via
    `shared.lm.factory.close_chat_model` once the pass is done (task #3915).
    """
    from shared.lm.factory import build_chat_model

    p = params or GenParams()
    return build_chat_model(model, reasoning_effort=p.reasoning_effort)


def generate_nodes(
    requests: list[GenRequest],
    *,
    model: str,
    llm: Any | None = None,
    params: GenParams | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    tools: Sequence[Any] | None = None,
) -> list[GenResult]:
    """Generate every request's text, concurrently; order preserved.

    `llm` is normally built by the caller (it owns timeout / provider
    options and the model's lifetime — a caller-supplied model is never
    closed here); when omitted, one is built from `model` here at
    `params.reasoning_effort` and closed again when the batch ends. A per-node
    failure (provider error after retries, or budget unfittable) is returned
    as that node's `error`, never raised — the caller retries those nodes on
    a later pass.

    `tools` is the agent's tool schema list (`[execute_code]`), passed by the
    worker / manual-build callers. With `tools` and a request `prefix` the
    call is agent-shaped (#4674): the prefix rides at the head unchanged, the
    material + prompt + text-only clause at the tail, and the tool schema
    stays bound so the leading bytes match the agent's own requests; a
    tool-call response is refused and re-invoked up to `params.tool_rounds`.
    Without them the material-only request is sent.

    Raises:
        ValueError: duplicate nids, unknown kind, or a non-positive
            `max_concurrent`; these are caller bugs, failing fast before any
            model call.
    """
    nids = [r.nid for r in requests]
    if len(set(nids)) != len(nids):
        raise ValueError("duplicate nids in requests — one result per node requires unique ids")
    for r in requests:
        if r.kind not in _PROMPTS:
            raise ValueError(
                f"request {r.nid!r}: unknown kind {r.kind!r} — expected {'/'.join(_PROMPTS)}"
            )
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    if not requests:
        return []
    p = params or GenParams()
    self_built = llm is None
    if self_built:
        llm = build_generation_llm(model, p)
    try:
        from shared.lm.factory import provider_key_of_model

        provider = provider_key_of_model(model)
        worker = partial(
            _generate_one,
            llm,
            model=model,
            provider=provider,
            params=p,
            retry_attempts=retry_attempts,
            tools=tools,
        )
        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            results = list(pool.map(worker, requests))
        failed = [r.nid for r in results if not r.ok]
        logger.info(
            "hierarchy generation: {ok}/{total} nodes ok, {failed} failed",
            ok=len(results) - len(failed),
            total=len(results),
            failed=len(failed),
        )
        if failed:
            logger.info("hierarchy generation failures: {failed}", failed=failed)
        return results
    finally:
        # Ownership: the batch closes what it built; a caller-supplied model
        # is the caller's to close (it may outlive the batch).
        if self_built:
            from shared.lm.factory import close_chat_model

            close_chat_model(llm)


def _generate_one(
    llm: Any,
    req: GenRequest,
    *,
    model: str,
    provider: str | None,
    params: GenParams,
    retry_attempts: int,
    tools: Sequence[Any] | None = None,
) -> GenResult:
    """Generate one node's text; failures become `GenResult.error`."""
    src_tok = count_tokens(req.input_text)
    budget = narrative_budget_tok(src_tok)
    if budget <= 0:
        return GenResult(
            nid=req.nid,
            src_tok=src_tok,
            budget_tok=budget,
            error=f"input below the budget floor ({src_tok} source tokens)",
        )
    prompt = build_prompt(req.kind, src_tok, params)
    try:
        text = _invoke_node(
            llm,
            req,
            prompt,
            tools=tools,
            desc=f"{model}, node {req.nid}",
            model=model,
            provider=provider,
            retry_attempts=retry_attempts,
            params=params,
        )
        text = clean_text(text)
        out_tok = count_tokens(text)
        if out_tok > budget:
            text, out_tok = _compress_toward_budget(
                llm,
                req.nid,
                text,
                out_tok,
                budget=budget,
                model=model,
                provider=provider,
                params=params,
                retry_attempts=retry_attempts,
            )
            if out_tok > budget:
                return GenResult(
                    nid=req.nid,
                    src_tok=src_tok,
                    budget_tok=budget,
                    out_tok=out_tok,
                    error=(
                        f"over budget after {params.compress_attempts} compression "
                        f"attempt(s): best {out_tok} > {budget} tokens"
                    ),
                )
        return GenResult(
            nid=req.nid,
            src_tok=src_tok,
            budget_tok=budget,
            out_tok=out_tok,
            text=text,
        )
    except GenerateError as e:
        return GenResult(nid=req.nid, src_tok=src_tok, budget_tok=budget, error=str(e))


def _invoke_node(
    llm: Any,
    req: GenRequest,
    prompt: str,
    *,
    tools: Sequence[Any] | None,
    desc: str,
    model: str,
    provider: str | None,
    retry_attempts: int,
    params: GenParams,
) -> str:
    """One node's model call — agent-shaped when prefix and tools are present.

    Agent-shaped (#4674): the request is `[*prefix, HumanMessage(material +
    prompt + text-only clause)]` through `llm.bind_tools(tools)` — the same
    leading bytes and tool schema the agent itself sends, so the provider
    serves the prefix from cache. A response carrying tool calls is refused
    with a ToolMessage error and re-invoked, up to `params.tool_rounds`
    refusal rounds; past that the node fails (never an unbounded loop).
    Without a prefix or tools the material-only request is sent.
    """
    if tools is None or not req.prefix:
        return invoke_text(
            llm,
            [{"type": "text", "text": req.input_text}, {"type": "text", "text": prompt}],
            desc=desc,
            error_type=GenerateError,
            retry_attempts=retry_attempts,
            provider=provider,
            model=model,
            usage_source="hierarchy.generate",
        )
    from langchain_core.messages import HumanMessage, ToolMessage

    bound = llm.bind_tools(list(tools))
    messages: list[Any] = [
        *req.prefix,
        HumanMessage(
            content=[
                {"type": "text", "text": req.input_text},
                {"type": "text", "text": prompt},
                {"type": "text", "text": _TEXT_ONLY_CLAUSE},
            ]
        ),
    ]
    limit = max(0, params.tool_rounds)
    rounds = 0
    while True:
        response = invoke_response(
            bound,
            messages,
            desc=desc,
            error_type=GenerateError,
            retry_attempts=retry_attempts,
            provider=provider,
            model=model,
            usage_source="hierarchy.generate",
        )
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        if not tool_calls:
            text = extract_text(response)
            if text:
                return text
            raise GenerateError(
                f"Model ({desc}) returned empty response (possible safety block). "
                f"response_metadata: {getattr(response, 'response_metadata', None)!r}"
            )
        if rounds >= limit:
            names = ", ".join(str(tc.get("name")) for tc in tool_calls)
            raise GenerateError(
                f"Model ({desc}) kept calling tools after {limit} refusal round(s) "
                f"({names}) — tools are unavailable in this call"
            )
        rounds += 1
        messages.append(response)
        messages.extend(
            ToolMessage(content=_TOOL_REFUSAL, tool_call_id=str(tc.get("id") or ""))
            for tc in tool_calls
        )


def _compress_toward_budget(
    llm: Any,
    nid: str,
    text: str,
    out_tok: int,
    *,
    budget: int,
    model: str,
    provider: str | None,
    params: GenParams,
    retry_attempts: int,
) -> tuple[str, int]:
    """Compress `text` toward `budget` tokens; returns the best candidate.

    Each attempt asks for `ask_ratio x budget` tokens worth of characters,
    scaled by the candidate's own observed chars/token ratio, and compresses
    its predecessor's output. Returns as soon as a candidate fits; when none
    does after all attempts, the best (shortest) candidate is still returned —
    the caller fails the node on it rather than writing an over-budget text.
    """
    best_text, best_tok = text, out_tok
    target_tok = int(budget * params.ask_ratio)
    for attempt in range(params.compress_attempts):
        ask = int(len(best_text) * target_tok / max(best_tok, 1))
        if attempt:
            ask = int(ask * params.compress_retry_scale)
        prompt = _COMPRESS_PROMPT.replace("{n}", str(ask))
        text = clean_text(
            invoke_text(
                llm,
                [{"type": "text", "text": best_text}, {"type": "text", "text": prompt}],
                desc=f"{model}, node {nid} compress",
                error_type=GenerateError,
                retry_attempts=retry_attempts,
                provider=provider,
                model=model,
                usage_source="hierarchy.generate",
            )
        )
        tok = count_tokens(text)
        if tok < best_tok:
            best_text, best_tok = text, tok
        if best_tok <= budget:
            break
    return best_text, best_tok
