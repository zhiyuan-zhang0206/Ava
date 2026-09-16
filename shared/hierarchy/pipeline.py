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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
class MaterializedNode:
    """One node's outcome, storage-ready."""

    nid: str
    level: int
    kind: str  # group | alias
    span: tuple[int, int]
    at: tuple[str, str]
    trigger: str
    src_tok: int
    text: str
    text_hash: str  # sha256 of the text (idempotent-write key)
    input_hash: str  # the generation cache key this text was produced for
    children: tuple[str, ...]  # child uids, in stream order


@dataclass(frozen=True)
class MaterializedTree:
    """A full run's outcome: what got text, what failed, what stays pending."""

    nodes: tuple[MaterializedNode, ...]
    errors: tuple[GenResult, ...]
    pending: dict[int, tuple[Unit, ...]]
    max_level: int


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
) -> MaterializedTree:
    """Generate every node of the sealed tree, level by level.

    `known_texts` maps `input_hash -> text` (the storage's reuse cache): a node
    whose input hash is known reuses its text without a model call. A node with
    a failed child has no input to build and fails without a call — its error
    names the missing child.
    """
    known = known_texts or {}
    texts: dict[str, str] = {}
    nodes: list[MaterializedNode] = []
    errors: list[GenResult] = []

    for level in range(1, sealed.max_level + 1):
        specs = [spec for spec in sealed.nodes if spec.level == level]
        requests: list[GenRequest] = []
        request_by_nid: dict[str, NodeSpec] = {}
        request_key_by_nid: dict[str, str] = {}
        for spec in specs:
            if spec.kind == "alias":
                child = spec.units[0]
                child_text = texts.get(child.uid)
                if child_text is None and child.uid in blocks_by_uid:
                    # A single oversized block aliases its rendered text upward
                    # (seal.py's source guard); blocks have no generated text.
                    child_text = render_block(msgs, blocks_by_uid[child.uid], render_params).text
                if child_text is None:
                    errors.append(
                        _error_result(
                            spec, f"child text missing (generation failed earlier): {child.uid}"
                        )
                    )
                    continue
                nodes.append(_materialized(spec, child_text, kind_of_input="alias"))
                texts[spec.nid] = child_text
                continue
            input_text, problem = _assemble_group_input(
                spec, msgs, blocks_by_uid, texts, render_params
            )
            if problem:
                errors.append(_error_result(spec, problem))
                continue
            key = input_hash("leaf" if level == 1 else "node", input_text)
            cached = known.get(key)
            if cached is not None:
                nodes.append(
                    _materialized(
                        spec, cached, kind_of_input="leaf" if level == 1 else "node", key=key
                    )
                )
                texts[spec.nid] = cached
                continue
            requests.append(
                GenRequest(
                    nid=spec.nid,
                    kind="leaf" if level == 1 else "node",
                    input_text=input_text,
                )
            )
            request_by_nid[spec.nid] = spec
            request_key_by_nid[spec.nid] = key
        if not requests:
            continue
        results = generate_nodes(
            requests,
            model=model,
            llm=llm,
            params=gen_params,
            max_concurrent=max_concurrent,
            retry_attempts=retry_attempts,
        )
        for result in results:
            if result.ok and result.text is not None:
                spec = request_by_nid[result.nid]
                nodes.append(
                    _materialized(
                        spec,
                        result.text,
                        kind_of_input="leaf" if spec.level == 1 else "node",
                        key=request_key_by_nid[result.nid],
                    )
                )
                texts[spec.nid] = result.text
            else:
                errors.append(result)

    return MaterializedTree(
        nodes=tuple(nodes),
        errors=tuple(errors),
        pending=sealed.pending,
        max_level=sealed.max_level,
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
) -> MaterializedTree:
    """One full run for an agent over its retained checkpoint history.

    `include_tail=True` (the default) treats "now" as a day-boundary backstop
    trigger and seals the trailing batch; the compact-driven worker passes
    False so unfinished stretches stay pending for the next compact.
    """
    msgs = load_checkpoint_messages_full(agent_id)
    items, _ = build_timeline_items(msgs, [])
    blocks = fold_blocks(items)
    units, table = build_units(msgs, blocks, render_params)
    batches = trigger_batches(items, units, include_tail=include_tail)
    sealed = seal_cascade([(b.name, list(b.units)) for b in batches], seal_params)
    return materialize(
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
    )
