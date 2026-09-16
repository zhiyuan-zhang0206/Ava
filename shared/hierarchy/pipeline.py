"""Tree assembly — messages to a materialized understanding tree.

The pipeline chains the engine modules into one run over an agent's retained
history:
1. `build_timeline_items` (the console item stream) then `fold_blocks` -> level-0 blocks;
2. blocks become units with rendered token sizes (`render.render_block`);
3. `trigger_batches` cuts the unit stream into seal batches at compact
   completions (the day-boundary backstop passes `include_tail=True`);
4. `seal_cascade` cuts sealable groups level by level;
5. `materialize` walks the levels from the bottom: leaves render their blocks,
   upper nodes reduce their children's texts, aliases copy their child's text.
   Within a level, nodes generate newest-span first in bounded chunks; a
   `deadline` stops generation cleanly between chunks (the worker's job
   budget), leaving the unattempted remainder (`skipped`) to the next run —
   which resumes from the reuse cache and redoes nothing.

Model calls happen only for nodes whose input hash is not in `known_texts` (the
storage layer's reuse cache: `input_hash -> text`), so a rerun over unchanged
history makes zero calls — the "rerun costs nothing" contract without storing
any transient state.

Determinism boundary (see `seal.py`): the tree STRUCTURE is
f(unit stream, trigger positions); the text is an LLM product behind the budget
checks. Everything before the model call is pure, so identical input always
yields identical requests — the machine-checkable half of the acceptance.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from langchain_core.messages import BaseMessage

from shared.checkpoint import load_checkpoint_messages_full
from shared.hierarchy.blocks import COMPACT_ITEM_KINDS, Block, fold_blocks
from shared.hierarchy.generate import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_RETRY_ATTEMPTS,
    GenParams,
    GenRequest,
    GenResult,
    generate_nodes,
    input_hash,
    text_hash,
)
from shared.hierarchy.nodes import MaterializedNode
from shared.hierarchy.render import RenderParams, render_block
from shared.hierarchy.seal import (
    NodeSpec,
    SealParams,
    SealResult,
    Unit,
    narrative_budget_tok,
    seal_cascade,
)
from shared.timeline import TimelineItem, build_timeline_items


@dataclass(frozen=True)
class TriggerBatch:
    """One seal batch: the units accumulated since the previous trigger."""

    name: str  # compact@i<idx> | tail
    units: tuple[Unit, ...]


@dataclass(frozen=True)
class MaterializedTree:
    """A full run's outcome: what got text, what failed, what stays pending."""

    nodes: tuple[MaterializedNode, ...]
    errors: tuple[GenResult, ...]
    pending: dict[int, tuple[Unit, ...]]
    max_level: int
    # Run-scope stats for the build job (task #3704 P2b): the trigger batches
    # walked (`batches`), nodes written from a model call vs the reuse cache,
    # nodes a deadline left unattempted (`skipped`; 0 = the run walked the
    # whole sealed tree), and the token sums of the generation attempts
    # (compression retries excluded — the llm usage ledger is authoritative).
    batches: int = 0
    generated: int = 0
    reused: int = 0
    skipped: int = 0
    src_tokens: int = 0
    out_tokens: int = 0


def trigger_batches(
    items: Sequence[TimelineItem], units: Sequence[Unit], *, include_tail: bool
) -> list[TriggerBatch]:
    """Cut the unit stream into seal batches at compact-completion triggers.

    A compact item closes the batch of every unit that ends before it. The
    trailing units (no trigger yet) form the `tail` batch only when
    `include_tail` — manual runs and the day-boundary backstop do; the
    compact-driven worker leaves them pending for the next trigger.
    """
    batches: list[TriggerBatch] = []
    cur: list[Unit] = []
    ptr = 0
    for item in items:
        if item.kind not in COMPACT_ITEM_KINDS:
            continue
        trigger_msg = int(item.item_id.split(".")[0])
        while ptr < len(units) and units[ptr].span[1] < trigger_msg:
            cur.append(units[ptr])
            ptr += 1
        if cur:
            batches.append(TriggerBatch(name=f"compact@i{trigger_msg}", units=tuple(cur)))
            cur = []
    while ptr < len(units):
        cur.append(units[ptr])
        ptr += 1
    if cur and include_tail:
        batches.append(TriggerBatch(name="tail", units=tuple(cur)))
    return batches


def build_units(
    msgs: Sequence[BaseMessage], blocks: Sequence[Block], params: RenderParams | None = None
) -> tuple[list[Unit], dict[str, Block]]:
    """Block units (uid/tok/span/at) plus the uid -> block lookup.

    A block that renders nothing (only ambient-context messages) carries no
    content and is dropped — it cannot contribute to any summary.
    """
    units: list[Unit] = []
    table: dict[str, Block] = {}
    for block in blocks:
        rendered = render_block(msgs, block, params)
        if rendered.tokens == 0:
            continue
        uid = f"b{block.i0}"
        units.append(
            Unit(
                uid=uid,
                tok=rendered.tokens,
                span=(block.i0, block.i1),
                at=(rendered.t0, rendered.t1),
                kind="block",
            )
        )
        table[uid] = block
    return units, table


def materialize(
    msgs: Sequence[BaseMessage],
    sealed: SealResult,
    blocks_by_uid: Mapping[str, Block],
    *,
    llm: Any,
    model: str,
    known_texts: Mapping[str, str] | None = None,
    render_params: RenderParams | None = None,
    gen_params: GenParams | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    deadline: float | None = None,
) -> MaterializedTree:
    """Generate the sealed tree's nodes, level by level, newest stretch first.

    Levels are strictly bottom-up (an upper node's input is its children's
    texts). Within a level the nodes are independent, so they generate
    newest-span first and in bounded chunks: a run cut short still lands its
    coverage on the newest window — the priority the compact-driven worker's
    slicing relies on (review, 3187), and a rerun skips everything already
    materialized via the reuse cache, so nothing is ever redone.

    `deadline` is a `time.monotonic` reading; when set, generation checks it
    before each chunk and stops cleanly between chunks once passed — the
    remaining nodes of that level and of every higher level count into
    `skipped` and wait for a continuation run. `None` = run to completion.

    `known_texts` maps `input_hash -> text` (the storage's reuse cache): a node
    whose input hash is known reuses its text without a model call (counted in
    `reused`). A node with a failed child has no input to build and fails
    without a call — its error names the missing child.
    """
    known = known_texts or {}
    texts: dict[str, str] = {}
    nodes: list[MaterializedNode] = []
    errors: list[GenResult] = []
    generated = reused = failed_nonalias = src_tokens = out_tokens = 0
    # One generate_nodes call per chunk; the chunk bounds only how often the
    # deadline is checked (a chunk ≈ max_concurrent * 4 model calls), never
    # the per-call fan-out itself.
    chunk_size = max(1, max_concurrent * 4)

    for level in range(1, sealed.max_level + 1):
        plan = _classify_level_specs(
            [spec for spec in sealed.nodes if spec.level == level],
            level,
            msgs,
            blocks_by_uid,
            texts,
            known,
            render_params,
        )
        nodes.extend(plan.nodes)
        errors.extend(plan.errors)
        reused += plan.cached
        failed_nonalias += plan.failed
        run = _run_chunks(
            plan.queued,
            texts=texts,
            llm=llm,
            model=model,
            gen_params=gen_params,
            max_concurrent=max_concurrent,
            retry_attempts=retry_attempts,
            chunk_size=chunk_size,
            deadline=deadline,
        )
        nodes.extend(run.nodes)
        errors.extend(run.errors)
        generated += run.generated
        failed_nonalias += run.failed
        src_tokens += run.src_tokens
        out_tokens += run.out_tokens
        if run.stopped:
            break

    # A node counts as covered when it materialized (generated or cache-hit) or
    # failed as a non-alias spec; everything else — never reached because the
    # deadline stopped generation, at this level or above — is `skipped` and
    # waits for the continuation run.
    total_nonalias = sum(1 for spec in sealed.nodes if spec.kind != "alias")
    return MaterializedTree(
        nodes=tuple(nodes),
        errors=tuple(errors),
        pending=sealed.pending,
        max_level=sealed.max_level,
        generated=generated,
        reused=reused,
        skipped=total_nonalias - generated - reused - failed_nonalias,
        src_tokens=src_tokens,
        out_tokens=out_tokens,
    )


class _LevelPlan(NamedTuple):
    """One level's classified work before generation.

    `nodes` are materialized already (aliases and reuse-cache hits), `errors`
    are spec-level failures (a missing child, an unbuildable input), `queued`
    are the (request, spec, cache-key) triples to generate — and `cached` /
    `failed` count their non-alias members for the run's stats.
    """

    nodes: list[MaterializedNode]
    errors: list[GenResult]
    queued: list[tuple[GenRequest, NodeSpec, str]]
    cached: int
    failed: int


def _classify_level_specs(
    specs: list[NodeSpec],
    level: int,
    msgs: Sequence[BaseMessage],
    blocks_by_uid: Mapping[str, Block],
    texts: dict[str, str],
    known: Mapping[str, str],
    render_params: RenderParams | None,
) -> _LevelPlan:
    """Classify one level's specs: materialize what needs no model call, queue
    what does. Alias specs materialize inline (their child's text); a group
    spec either hits the reuse cache, fails for lack of input, or queues."""
    kind_of_input = "leaf" if level == 1 else "node"
    plan_nodes: list[MaterializedNode] = []
    plan_errors: list[GenResult] = []
    queued: list[tuple[GenRequest, NodeSpec, str]] = []
    cached = failed = 0
    for spec in specs:
        if spec.kind == "alias":
            child = spec.units[0]
            child_text = texts.get(child.uid)
            if child_text is None and child.uid in blocks_by_uid:
                # A single oversized block aliases its rendered text upward
                # (seal.py's source guard); blocks have no generated text.
                child_text = render_block(msgs, blocks_by_uid[child.uid], render_params).text
            if child_text is None:
                plan_errors.append(
                    _error_result(
                        spec, f"child text missing (generation failed earlier): {child.uid}"
                    )
                )
                continue
            plan_nodes.append(_materialized(spec, child_text, kind_of_input="alias"))
            texts[spec.nid] = child_text
            continue
        input_text, problem = _assemble_group_input(spec, msgs, blocks_by_uid, texts, render_params)
        if problem:
            plan_errors.append(_error_result(spec, problem))
            failed += 1
            continue
        key = input_hash(kind_of_input, input_text)
        cached_text = known.get(key)
        if cached_text is not None:
            cached += 1
            plan_nodes.append(
                _materialized(spec, cached_text, kind_of_input=kind_of_input, key=key)
            )
            texts[spec.nid] = cached_text
            continue
        queued.append(
            (
                GenRequest(nid=spec.nid, kind=kind_of_input, input_text=input_text),
                spec,
                key,
            )
        )
    return _LevelPlan(
        nodes=plan_nodes, errors=plan_errors, queued=queued, cached=cached, failed=failed
    )


class _ChunkRun(NamedTuple):
    """One level's generation outcome across its chunks."""

    nodes: list[MaterializedNode]
    errors: list[GenResult]
    generated: int
    failed: int
    src_tokens: int
    out_tokens: int
    stopped: bool


def _run_chunks(
    queued: list[tuple[GenRequest, NodeSpec, str]],
    *,
    texts: dict[str, str],
    llm: Any,
    model: str,
    gen_params: GenParams | None,
    max_concurrent: int,
    retry_attempts: int,
    chunk_size: int,
    deadline: float | None,
) -> _ChunkRun:
    """Generate `queued` in newest-first chunks, stopping cleanly between chunks
    once `deadline` (a monotonic reading) has passed."""
    # Newest first inside the level — the order is free within a level and the
    # newest window is what a viewer needs covered first.
    queued = sorted(queued, key=lambda item: item[1].span, reverse=True)
    run_nodes: list[MaterializedNode] = []
    run_errors: list[GenResult] = []
    generated = failed = src_tokens = out_tokens = 0
    stopped = False
    for start in range(0, len(queued), chunk_size):
        if deadline is not None and time.monotonic() > deadline:
            stopped = True
            break
        chunk = queued[start : start + chunk_size]
        spec_by_nid = {request.nid: spec for request, spec, _ in chunk}
        key_by_nid = {request.nid: key for request, _, key in chunk}
        results = generate_nodes(
            [request for request, _, _ in chunk],
            model=model,
            llm=llm,
            params=gen_params,
            max_concurrent=max_concurrent,
            retry_attempts=retry_attempts,
        )
        for result in results:
            src_tokens += result.src_tok
            out_tokens += result.out_tok or 0
            spec = spec_by_nid[result.nid]
            if result.ok and result.text is not None:
                generated += 1
                run_nodes.append(
                    _materialized(
                        spec,
                        result.text,
                        kind_of_input="leaf" if spec.level == 1 else "node",
                        key=key_by_nid[result.nid],
                    )
                )
                texts[spec.nid] = result.text
            else:
                run_errors.append(result)
                failed += 1
    return _ChunkRun(
        nodes=run_nodes,
        errors=run_errors,
        generated=generated,
        failed=failed,
        src_tokens=src_tokens,
        out_tokens=out_tokens,
        stopped=stopped,
    )


def _header(spec: NodeSpec) -> str:
    """The input's leading line: identity, span, and size orientation."""
    if spec.level == 1:
        label = f"{len(spec.units)} source blocks"
    else:
        label = f"reduced from {len(spec.units)} child summaries"
    return (
        f"# node {spec.nid} - {label}, i{spec.span[0]}-i{spec.span[1]}, "
        f"{spec.at[0]} -> {spec.at[1]}\n\n"
    )


def _child_section(unit: Unit, text: str) -> str:
    """One child's section inside an upper node's input."""
    return f"## [{unit.uid}] i{unit.span[0]}-i{unit.span[1]}, {unit.at[0]} -> {unit.at[1]}\n\n{text}\n\n"


def _assemble_group_input(
    spec: NodeSpec,
    msgs: Sequence[BaseMessage],
    blocks_by_uid: Mapping[str, Block],
    texts: Mapping[str, str],
    render_params: RenderParams | None,
) -> tuple[str, str]:
    """Assemble one group node's input; returns `(input_text, problem)`.

    Exactly one side is non-empty: a leaf renders its child blocks, an upper
    node concatenates its children's texts; a missing child yields the
    problem (no model call — there is no input to make one from).
    """
    if spec.level == 1:
        missing_blocks = [u.uid for u in spec.units if u.uid not in blocks_by_uid]
        if missing_blocks:
            return "", f"missing block unit(s): {', '.join(missing_blocks)}"
        body = "".join(
            render_block(msgs, blocks_by_uid[u.uid], render_params).text for u in spec.units
        )
    else:
        missing = [u.uid for u in spec.units if u.uid not in texts]
        if missing:
            return "", f"child text missing (generation failed earlier): {', '.join(missing)}"
        body = "".join(_child_section(u, texts[u.uid]) for u in spec.units)
    return _header(spec) + body, ""


def _error_result(spec: NodeSpec, error: str) -> GenResult:
    return GenResult(
        nid=spec.nid,
        src_tok=spec.src_tok,
        budget_tok=narrative_budget_tok(spec.src_tok),
        error=error,
    )


def _materialized(
    spec: NodeSpec, text: str, *, kind_of_input: str, key: str | None = None
) -> MaterializedNode:
    return MaterializedNode(
        nid=spec.nid,
        level=spec.level,
        kind=spec.kind,
        span=spec.span,
        at=spec.at,
        trigger=spec.trigger,
        src_tok=spec.src_tok,
        text=text,
        text_hash=text_hash(text),
        input_hash=key if key is not None else input_hash(kind_of_input, text),
        children=tuple(u.uid for u in spec.units),
        children_spans=tuple(u.span for u in spec.units),
    )


def build_agent_tree(
    agent_id: int,
    *,
    llm: Any,
    model: str,
    include_tail: bool = True,
    known_texts: Mapping[str, str] | None = None,
    render_params: RenderParams | None = None,
    gen_params: GenParams | None = None,
    seal_params: SealParams | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    deadline: float | None = None,
) -> MaterializedTree:
    """One full run for an agent over its retained checkpoint history.

    `include_tail=True` (the default) treats "now" as a day-boundary backstop
    trigger and seals the trailing batch; the compact-driven worker passes
    False so unfinished stretches stay pending for the next compact.

    `deadline` (a `time.monotonic` reading) bounds the generation pass only —
    load/fold/seal always run whole. A run stopped at the deadline writes the
    nodes it produced (`skipped` names the remainder) and a continuation run
    resumes from the reuse cache without redoing any of them.
    """
    msgs = load_checkpoint_messages_full(agent_id)
    items, _ = build_timeline_items(msgs, [])
    blocks = fold_blocks(items)
    units, table = build_units(msgs, blocks, render_params)
    batches = trigger_batches(items, units, include_tail=include_tail)
    sealed = seal_cascade([(b.name, list(b.units)) for b in batches], seal_params)
    tree = materialize(
        msgs,
        sealed,
        table,
        llm=llm,
        model=model,
        known_texts=known_texts,
        render_params=render_params,
        gen_params=gen_params,
        max_concurrent=max_concurrent,
        retry_attempts=retry_attempts,
        deadline=deadline,
    )
    return replace(tree, batches=len(batches))
