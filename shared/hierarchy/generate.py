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

Calibration provenance for every constant here: the v0.3 demo's machine check
(task #3704; 72 nodes, 0 over budget across 3 levels).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

from loguru import logger

from shared.hierarchy.seal import narrative_budget_tok
from shared.hierarchy.tokens import count_tokens
from shared.lm._call import invoke_text

# Bump when the prompt templates change: stored rows record the version they
# were generated with, so a text-quality question can be traced to a template.
PROMPT_VERSION = "0.3"

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
    shape as `seal.SealParams`; deployment policy (which model) lives in
    config (`settings.lm.hierarchy_model`).
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


@dataclass(frozen=True)
class GenRequest:
    """One node to generate: its stable id, kind, and input material."""

    nid: str
    kind: str  # leaf | node
    input_text: str


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


def generate_nodes(
    requests: list[GenRequest],
    *,
    model: str,
    llm: Any | None = None,
    params: GenParams | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> list[GenResult]:
    """Generate every request's text, concurrently; order preserved.

    `llm` is normally built by the caller (it owns timeout / provider
    options); when omitted, one is built from `model` here at
    `params.reasoning_effort`. A per-node failure (provider error after
    retries, or budget unfittable) is returned as that node's `error`, never
    raised — the caller retries those nodes on a later pass.

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
    if llm is None:
        from shared.lm.factory import build_chat_model

        llm = build_chat_model(model, reasoning_effort=p.reasoning_effort)
    from shared.lm.factory import provider_key_of_model

    provider = provider_key_of_model(model)
    worker = partial(
        _generate_one,
        llm,
        model=model,
        provider=provider,
        params=p,
        retry_attempts=retry_attempts,
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


def _generate_one(
    llm: Any,
    req: GenRequest,
    *,
    model: str,
    provider: str | None,
    params: GenParams,
    retry_attempts: int,
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
        text = invoke_text(
            llm,
            [{"type": "text", "text": req.input_text}, {"type": "text", "text": prompt}],
            desc=f"{model}, node {req.nid}",
            error_type=GenerateError,
            retry_attempts=retry_attempts,
            provider=provider,
            model=model,
            usage_source="hierarchy.generate",
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
