"""Context-breakdown: pure bucketing/section-split/normalization functions
(`gateway/context_breakdown.py`) + the `GET /api/agents/{id}/context-breakdown`
endpoint contract.
"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# Load agent.graph before agent.hooks.compact to resolve the latent graph<->compact
# import cycle (compact.py imports agent.graph._context; _claim imports back from
# compact). Needed only because this test uses the write-side helper below —
# gateway.context_breakdown itself must NOT need it (see
# test_bucket_messages_works_without_agent_graph).
import agent.graph  # noqa: F401  # pyright: ignore[reportUnusedImport]
from agent.hooks.compact import compose_summary_message
from agent.messages import NoteTag, inbound_message, system_note_message
from gateway.app import app
from gateway.context_breakdown import (
    SECTION_SPLIT_THRESHOLD_TOKENS,
    SectionNode,
    bucket_messages,
    compute_breakdown,
    normalize,
    section_breakdown,
)
from shared.db import create_agent


def _assert_conserved(node: SectionNode) -> None:
    """Every internal node's tokens equal the sum of its children's (the residual
    prose is itself a leaf child), recursively."""
    if node.children:
        assert node.tokens == sum(c.tokens for c in node.children), node
        for child in node.children:
            _assert_conserved(child)


def _find(nodes: list[SectionNode], name: str) -> SectionNode:
    return next(n for n in nodes if n.name == name)


def _note(content: str, tag: NoteTag) -> HumanMessage:
    return system_note_message(content=content, tag=tag, created_at=datetime.now(UTC))


def _sample_messages() -> list:
    return [
        SystemMessage(content="intro paragraph\n\n# Tools\ntool stuff\n\n# Skills\nskill stuff"),
        inbound_message(content="hello there", source="user", inbound_id=1),
        inbound_message(content="peer agent says hi", source="agent:5", inbound_id=2),
        inbound_message(content="watcher woke you", source="watcher:3", inbound_id=3),
        _note("cluster memory pointer", NoteTag.MEMORY),
        _note("per-agent memory", NoteTag.AGENT_MEMORY),
        _note("your agent id is 5", NoteTag.AGENT_ID),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "some reasoning"},
                {"type": "text", "text": "the reply"},
            ],
            tool_calls=[{"name": "execute_code", "args": {"code": "print(1)"}, "id": "c1"}],
            usage_metadata={"input_tokens": 1000, "output_tokens": 40, "total_tokens": 1040},
        ),
        ToolMessage(content="tool output", tool_call_id="c1"),
        HumanMessage(content=compose_summary_message("a compact summary body")),
    ]


# ── pure functions ──────────────────────────────────────────────────────────


def test_bucket_messages_categorizes_every_kind() -> None:
    buckets, system_prompt = bucket_messages(_sample_messages())  # pyright: ignore[reportUnknownArgumentType]
    # Every distinct kind present is bucketed; note tags split correctly, the
    # AGENT_ID note is a generic context_note, the header-prefixed untagged
    # HumanMessage is the compact summary (not user_input). Inbounds split by
    # source: the human turn -> user_input, the peer agent -> agent_messages, the
    # watcher wakeup -> automation.
    assert set(buckets) == {
        "system_prompt",
        "user_input",
        "agent_messages",
        "automation",
        "cluster_memory",
        "agent_memory",
        "context_note",
        "reasoning",
        "output",
        "tool_call",
        "tool_response",
        "compact_summary",
    }
    assert system_prompt.startswith("intro paragraph")
    # Split keys on ava_source metadata, not the content prefix; the test passes
    # raw (unwrapped) content, so each bucket is that string's length.
    assert buckets["user_input"] == len("hello there")  # only the human inbound
    assert buckets["agent_messages"] == len("peer agent says hi")
    assert buckets["automation"] == len("watcher woke you")
    assert buckets["reasoning"] == len("some reasoning")
    assert buckets["output"] == len("the reply")
    assert buckets["tool_call"] == len("print(1)")


def test_bucket_ignores_image_base64_in_multimodal_inbound() -> None:
    """A multimodal inbound counts only its text block — never the base64 image
    (which becomes image tokens, not char tokens)."""
    huge_b64 = "A" * 100_000
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "data": huge_b64}},
        ],
        additional_kwargs={"ava_msg_type": "inbound"},
    )
    buckets, _ = bucket_messages([msg])
    assert buckets["user_input"] == len("look at this")  # base64 excluded


def test_inbound_split_by_source() -> None:
    """Inbound messages are bucketed by their envelope `ava_source`: a peer agent
    (`agent:N`) -> agent_messages; machine/framework wakeups & notices
    (`watcher:` / `shell:` / `schedule:` / `system[:*]`) -> automation; a human
    turn (`user` / `ui:page:*`) and a legacy inbound with no source -> user_input."""
    msgs = [
        inbound_message(content="human", source="user", inbound_id=1),
        inbound_message(content="page", source="ui:page:dash", inbound_id=2),
        inbound_message(content="peer", source="agent:7", inbound_id=3),
        inbound_message(content="wake", source="watcher:2", inbound_id=4),
        inbound_message(content="shell done", source="shell:9", inbound_id=5),
        inbound_message(content="sched", source="schedule:4", inbound_id=6),
        inbound_message(content="sys", source="system", inbound_id=7),
        inbound_message(content="sys sub", source="system:reconcile", inbound_id=8),
        # legacy inbound with no ava_source -> defaults to user_input.
        HumanMessage(content="legacy", additional_kwargs={"ava_msg_type": "inbound"}),
    ]
    buckets, _ = bucket_messages(msgs)
    assert buckets["user_input"] == len("human") + len("page") + len("legacy")
    assert buckets["agent_messages"] == len("peer")
    assert buckets["automation"] == len("wake") + len("shell done") + len("sched") + len(
        "sys"
    ) + len("sys sub")


def test_section_breakdown_flat_below_threshold() -> None:
    """A small system prompt: the top-level `#` sections are listed as leaves —
    no recursion (every section is under the threshold), preamble first, `##`
    absorbed into its parent's residual. Tokens sum to the anchor."""
    content = "preamble line\n# Tools\ntool body\n## Subsection\nsub body\n# Skills\nskill body"
    nodes = section_breakdown(content, 500)
    assert [n.name for n in nodes] == ["(preamble)", "Tools", "Skills"]
    assert all(
        n.children == [] for n in nodes
    )  # ## Subsection did not surface (Tools <= threshold)
    assert sum(n.tokens for n in nodes) == 500
    for n in nodes:
        _assert_conserved(n)


def test_section_breakdown_empty() -> None:
    assert section_breakdown("", 500) == []


def _nested_prompt() -> str:
    # One dominant section (A, with two sub-headings whose bodies dwarf the rest)
    # plus a small B and a giant heading-less C. `A` and `A1` clear the threshold;
    # `A2`, `B`, `C`, and the residual prose stay leaves.
    return (
        "preamble text\n"
        "# A\n"
        + ("a " * 20 + "\n") * 2
        + "## A1\n"
        + ("x " * 60 + "\n") * 4
        + "## A2\n"
        + ("y " * 8 + "\n")
        + "# B\n"
        + ("b " * 4 + "\n")
        + "# C\n"
        + ("c " * 500 + "\n")  # giant, but no sub-headings -> stays a leaf
    )


def test_section_breakdown_recurses_above_threshold() -> None:
    nodes = section_breakdown(_nested_prompt(), 12_000)
    names = [n.name for n in nodes]
    assert names == ["(preamble)", "A", "B", "C"]

    a = _find(nodes, "A")
    assert a.tokens > SECTION_SPLIT_THRESHOLD_TOKENS
    assert a.children, "A is over the threshold with sub-headings -> must split"
    # A's residual prose (heading line + the 2 body lines before ## A1) surfaces
    # as an `(intro)` leaf child; A1/A2 are the real sub-sections.
    assert [c.name for c in a.children] == ["(intro)", "A1", "A2"]
    a2 = _find(a.children, "A2")
    assert a2.tokens <= SECTION_SPLIT_THRESHOLD_TOKENS and a2.children == []

    # A giant section with no sub-headings cannot be split -> leaf even when huge.
    c = _find(nodes, "C")
    assert c.tokens > SECTION_SPLIT_THRESHOLD_TOKENS and c.children == []


def test_section_breakdown_below_threshold_stays_leaf_despite_subheadings() -> None:
    """The threshold is on the (normalized) tokens: with a small anchor even a
    section that *has* sub-headings stays a leaf — recursion only kicks in over
    the threshold."""
    nodes = section_breakdown(_nested_prompt(), 400)
    assert all(n.children == [] for n in nodes)


def test_section_breakdown_recurses_to_depth_three() -> None:
    """A sub-section that is itself over the threshold and has deeper headings
    drills a third level."""
    content = (
        "# A\n"
        + ("a " * 4 + "\n")
        + "## A1\n"
        + ("x " * 4 + "\n")
        + "### A1a\n"
        + ("p " * 40 + "\n") * 3
        + "### A1b\n"
        + ("q " * 40 + "\n") * 3
        + "## A2\n"
        + ("y " * 3 + "\n")
    )
    nodes = section_breakdown(content, 9000)
    a1 = _find(_find(nodes, "A").children, "A1")
    assert a1.tokens > SECTION_SPLIT_THRESHOLD_TOKENS
    assert [c.name for c in a1.children] == ["(intro)", "A1a", "A1b"]


def test_section_breakdown_conserves_tokens_at_every_level() -> None:
    nodes = section_breakdown(_nested_prompt(), 12_000)
    assert sum(n.tokens for n in nodes) == 12_000  # top level sums to the anchor
    for n in nodes:
        _assert_conserved(n)  # and every parent == sum(children), recursively


def test_section_breakdown_falls_back_to_estimate_without_anchor() -> None:
    """No anchor (`system_prompt_tokens <= 0`): values are the chars/4 estimate,
    apportioned so the tree still conserves at every level."""
    nodes = section_breakdown(_nested_prompt(), 0)
    assert sum(n.tokens for n in nodes) > 0
    for n in nodes:
        _assert_conserved(n)


def test_normalize_sums_exactly_to_target() -> None:
    buckets = {"a": 300, "b": 200, "c": 100}  # est_total 600
    out = normalize(buckets, 1000)
    assert sum(out.values()) == 1000  # exact, residual absorbed by the largest
    assert out["a"] > out["b"] > out["c"]  # proportions preserved


def test_normalize_falls_back_to_chars_over_4_without_anchor() -> None:
    assert normalize({"a": 400, "b": 40}, 0) == {"a": 100, "b": 10}


def test_compute_breakdown_parts_sum_to_total() -> None:
    categories, sections, estimated_total = compute_breakdown(_sample_messages(), 1000)  # pyright: ignore[reportUnknownArgumentType]
    assert sum(t for _, t in categories) == 1000  # categories sum to the anchor
    system_prompt_tokens = dict(categories)["system_prompt"]
    # The section tree's top level sums to the system_prompt category, and every
    # parent conserves its children.
    assert sum(n.tokens for n in sections) == system_prompt_tokens
    for n in sections:
        _assert_conserved(n)
    assert estimated_total > 0


def test_bucket_messages_works_without_agent_graph() -> None:
    """Regression: a gateway process never imports agent.graph, and the breakdown
    must not pull it in. The old lazy `from agent.hooks.compact import
    COMPACT_SUMMARY_HEADER` inside bucket_messages hit the latent
    compact<->graph import cycle on first call in exactly that process shape,
    500-ing every GET /context-breakdown in prod (the in-process tests above
    stayed green only because they pre-import agent.graph). Run the call in a
    fresh interpreter to reproduce the gateway's import state."""
    code = (
        "import sys\n"
        "from langchain_core.messages import HumanMessage\n"
        "from gateway.context_breakdown import bucket_messages\n"
        "from agent.messages import COMPACT_SUMMARY_HEADER\n"
        "buckets, _ = bucket_messages("
        "[HumanMessage(content=f'{COMPACT_SUMMARY_HEADER}\\n\\nbody')])\n"
        "assert buckets == {'compact_summary': len(COMPACT_SUMMARY_HEADER) + 6}, buckets\n"
        "assert 'agent.graph' not in sys.modules, 'breakdown must not import agent.graph'\n"
    )
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root, check=False
    )
    assert proc.returncode == 0, proc.stderr


# ── endpoint ────────────────────────────────────────────────────────────────


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    with TestClient(app) as client:
        yield client


def _put_checkpoint(agent_id: int, messages: list) -> None:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.config import settings

    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def test_endpoint_returns_normalized_breakdown(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    tid = create_agent(db_conn)
    _put_checkpoint(tid, _sample_messages())

    resp = test_client.get(f"/api/agents/{tid}/context-breakdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_input_tokens"] == 1000  # the AIMessage's real input_tokens
    assert sum(c["tokens"] for c in body["categories"]) == 1000  # normalized to truth
    kinds = {c["kind"] for c in body["categories"]}
    assert {"system_prompt", "compact_summary", "cluster_memory", "reasoning"} <= kinds
    section_names = [s["name"] for s in body["sections"]]
    assert section_names == ["(preamble)", "Tools", "Skills"]
    # The recursive shape is on the wire: each node carries `children` (empty here
    # — this tiny prompt is under the split threshold).
    assert all(s["children"] == [] for s in body["sections"])
    assert body["estimated_total"] > 0


def test_endpoint_no_checkpoint_is_empty(
    db_conn: psycopg.Connection, test_client: TestClient
) -> None:
    """A just-created agent with no checkpoint -> empty breakdown, zeroed totals
    (tolerance contract: the dialog re-opens fine once the agent runs a turn)."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/context-breakdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_input_tokens"] == 0
    assert body["categories"] == []
    assert body["sections"] == []
