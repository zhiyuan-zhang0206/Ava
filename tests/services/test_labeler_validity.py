"""Labeler output-validity classification — the check that keeps a failed
generation out of the user-facing `agents.label`.

Issue #178: fed a long English second-person imperative brief (the shape
`ava.agents.spawn()` produces when an agent writes another agent's prompt),
`deepseek-v4-flash` failed every draw on the real preview-cluster prompts —
15/15 on replay — answering the brief in assistant voice, emitting `<think>` /
`<thinking>` scaffolding as plain text, and echoing the `<user_request>` fence
back. `_normalize` took the first 64 characters of each and
`generate_label_async` wrote it.

Three corpora pin the classifier, because a classifier that never fires and one
that fires on good labels are both failures — and the second is the one that
ships silently:

* `_ISSUE_178_OUTPUTS` — the nine real bad outputs recorded in #178. Every one
  must be rejected.
* `_PROD_LABELS` — a curated sample of the 287 real auto-generated labels on
  the production cluster (English and Chinese, verbatim; entries carrying user
  paths, hostnames or URLs excluded). Not one may be rejected. The full 287
  measured 0 false positives.
* `_FIRST_PERSON_PROMPT_LABELS` — real labels generated from prompts a user
  wrote in the FIRST PERSON. `assistant_voice` is the one rule keyed on register
  rather than structure, so it is the one that could eat a good label; these are
  its adversarial set. 150 such generations measured 0 false positives, because
  a correct summary of a first-person prompt does not stay in first person.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import redis.asyncio as aredis
from langchain_core.messages import AIMessage

import services.labeler.labeler as labeler_module
from services.labeler.labeler import (
    _LABEL_SYSTEM_PROMPT,
    LABEL_MAX_CHARS,
    _rejection_reason,
    generate_label_async,
)
from shared.db import create_agent

# The nine outputs #178 recorded from real runs: three observed landing in
# `agents.label` on the preview cluster, six from replaying the stored prompts
# through `_LABEL_SYSTEM_PROMPT`. All are already 64-char-truncated by
# `_normalize` — that is the exact string that reached the database.
_ISSUE_178_OUTPUTS = [
    "I'll systematically test the preview cluster's frontend as a ske",
    "I'll start by checking if the validation suite exists, then work",
    "<request_id>req_0190f9a2-3c5e-7f8a-9b1c-4d5e6f7a8b9c</request_id",
    "I'll test the preview cluster's frontend systematically. Let me ",
    "I'll validate the preview cluster's frontend systematically. Let",
    "<user_request>You are validating **core agent capabilities** on ",
    "<thinking>I need to summarize the user request into a label of a",
    "<think>I need to validate timezone settings and stress-test the ",
    "I'll validate the timezone configuration and then test the syste",
]

# Real production labels. Deliberately includes the machine-protocol ones that
# are exactly LABEL_MAX_CHARS long and contain `<yes|no>` placeholders, and the
# one that is a verbatim prefix of its own prompt: they are the measured
# counter-examples to the three rules this change considered and dropped (see
# `_rejection_reason`'s docstring).
_PROD_LABELS = [
    "Extend Syntax Fix Plugin to auto-import missing names on NameErr",
    "Mining transcript for user interrupt signals and durable prefere",
    "DRIVE_PROBE_RESULT mounted=<yes|no> writable=<yes|no> path=<abso",
    "DRIVE_PROBE_RESULT os=<your_os> writable=<yes_or_no> path=<absol",
    "DRIVE_PROBE_RESULT os=macos writable=no path=NONE note=no_Google",
    "DRIVE_PROBE_RESULT os=wsl writable=<yes|no> path=<absolute_writa",
    "Display worktree location and show its added Markdown files via ",
    "DRIVE_PROBE_RESULT",
    "online",
    "Greeting",
    "ack",
    "Terminate AV",
    "Check GPU su",
    "sidebar labe",
    "Show SDKs",
    "hi",
    "ping",
    "Pong",
    "pong",
    "wechat-daemon",
    "Fix agent communication system prompt",
    "feat: improve help discoverability and guidance",
    "Investigate Agent #80 crash cause and TypeError",
    "27__premature_solving_without_user_alignment.md",
    "16__NONE.md",
    "revert verification - terminating immediately",
    "Review PR #44 screen capture check",
    "Review PR #60 using auto-review skill",
    "Review PR #68 with auto-review skill",
    "Review PR #67 with auto-review",
    "\u4ec5\u4f5c\u4e3a\u603b\u6307\u6325",
    "\u524d\u7aefMarkdown\u6e32\u67d3\u6a2a\u5411\u6eda\u52a8\u5931\u6548\u95ee\u9898",
    "\u529f\u80fd\u89c4\u5212\u4e0e\u5f85\u529e\u68b3\u7406",
    "\u5b9e\u73b0agent\u6d3b\u52a8\u8ffd\u8e2a\u3001push\u3001PR\u4e0eCI\u76d1\u63a7",
    "\u5728 Sidebar \u4e2d\u6dfb\u52a0\u663e\u793a\u5df2\u505c\u6b62 agents \u7684\u5207\u6362\u5f00\u5173",
    "\u8bf7\u81ea\u884c\u5904\u7406preview\u96c6\u7fa4\u6d4b\u8bd5",
    "Agent\u6d88\u606f\u67b6\u6784\u8868\u683c\u6e32\u67d3\u5f02\u5e38\u6392\u67e5",
    "Google Drive\u7528\u6237\u884c\u4e3a\u6570\u636e\u5206\u6790\u4e0e\u89c6\u9891\u7406\u89e3",
    "\u97f3\u9891\u8f6c\u5f55",
    "\u5ba1\u67e5PR #54\u5e76\u6267\u884c\u81ea\u52a8\u4ee3\u7801\u5ba1\u67e5",
    "\u4e0b\u8f7dClaude\u914d\u7f6e\u4fdd\u5b58\u5230\u672c\u5730",
    "\u8bfb\u53d6\u5e76\u9a8c\u8bc1\u58f0\u660e\u540e\u5199\u5165JSON",
    "\u5c06JSON\u5199\u5165\u6587\u4ef6\u5e76\u7ec8\u6b62",
    "\u5ba1\u8ba1 SWE-bench runner \u6539\u4e3a\u6d41\u5f0f\u8f93\u51fa\u7684\u4ee3\u7801\u6539\u52a8\u70b9",
    "\u521b\u5efaAI agent\u6df1\u5ea6\u5bf9\u6bd4\u8868\u5e76\u4ea7\u51faHTML\u9875\u9762",
    "Fix missing page template",
    "\u4e3aenum\u589e\u52a0Jina Reader\u5347\u7ea7\u4ee5\u9002\u914d\u4eba\u6c11\u7f51\u6293\u53d6",
    "\u6e05\u7406config_overlay\u4e2dskills_to_inject_into_system_prompt\u7684\u65e7\u683c\u5f0fskill\u540d",
    "\u8c03\u67e5\u96c6\u7fa4\u62d3\u6251\u4e0e\u5355\u673a\u591acluster\u95ee\u9898",
    "\u4ee3\u7801\u5c42\u5168\u91cf\u91cd\u547d\u540d\u539f\u751f\u8f85\u52a9\u5de5\u5177\u4e3aava-desktop-helper",
    "\u4fee\u590dpg_tools._free_port\u7684TOCTOU\u7ade\u6001\u95ee\u9898",
    "\u9489\u4f4fchrome-devtools-mcp\u7248\u672c\u5e76\u5ba1\u8ba1\u672a\u9489\u4f9d\u8d56",
    "\u8c03\u67e5\u6d88\u606f\u6295\u9012\u5ef6\u8fdf\u4e0e\u8f6e\u8be2\u673a\u5236\u5ba1\u8ba1",
]


# Real labels the model produced for first-person user prompts ("I'll need the
# deploy checklist reviewed before Friday's release" -> "Deploy checklist review
# before Friday release"). The adversarial set for `assistant_voice`: if a
# future widening of the opener pattern starts eating these, it is eating real
# labels.
_FIRST_PERSON_PROMPT_LABELS = [
    "Deploy checklist review before Friday release",
    "WebSocket reconnect loop debugging in gateway",
    "500 on /api/agents when spawning without prompt",
    "Rewrite retry logic to fix double-counted attempts",
    "Migration completion notice and row counts",
    "Notify when migration done and paste row counts",
    "PgBouncer listener bind failure cause",
    "Hand off labeler PR to whoever picks it up",
    "Labeler display name decision logic",
    "Fix pyright false missing symbol reports",
    "Worktree broken pyright false positive symbols",
    "Merge queue config changes last month",
    "Greeting from Windows",
    "Windows greeting",
    "Reply hello from Windows",
    "Write JSON and terminate",
    "spawn\u62a5\u9519agent\u8d77\u4e0d\u6765",
    "\u8bca\u65adspawn\u62a5\u9519agent\u65e0\u6cd5\u542f\u52a8",
    "labeler \u4e3a\u4ec0\u4e48\u7ed9 agent \u8d77\u602a\u540d\u5b57",
    "\u8be2\u95ee labeler \u547d\u540d agent \u7684\u539f\u56e0",
    "\u6807\u7b7e\u5668\u5982\u4f55\u51b3\u5b9a\u667a\u80fd\u4f53\u663e\u793a\u540d",
]


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, _messages: list[Any]) -> AIMessage:
        return AIMessage(content=self.content)


@pytest.fixture
def _no_publish(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture (and suppress) the redis publish so a label write is observable
    without a live events channel."""
    published: list[str] = []

    async def _capture(_self: Any, _channel: str, payload: str) -> int:
        published.append(payload)
        return 1

    monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)
    return published


def _label_of(conn: psycopg.Connection, agent_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


class TestRejectionReason:
    """The classifier pinned directly, from both sides."""

    @pytest.mark.parametrize("raw", _ISSUE_178_OUTPUTS)
    def test_rejects_every_real_bad_output(self, raw: str) -> None:
        assert _rejection_reason(raw) is not None

    @pytest.mark.parametrize("label", _PROD_LABELS)
    def test_accepts_every_real_production_label(self, label: str) -> None:
        assert _rejection_reason(label) is None, f"false positive on a real label: {label!r}"

    @pytest.mark.parametrize("label", _FIRST_PERSON_PROMPT_LABELS)
    def test_accepts_labels_summarized_from_first_person_prompts(self, label: str) -> None:
        """`assistant_voice` is keyed on register, so this is the corpus that
        would expose it eating good labels."""
        assert _rejection_reason(label) is None, f"false positive on a real label: {label!r}"

    def test_rejects_assistant_voice_behind_an_interjection(self) -> None:
        """ "Sure, I'll ..." — the first-person marker is not at position 0, and
        an opener test anchored past the interjection would miss it."""
        assert _rejection_reason("Sure, I'll take a look at the migration") == "assistant_voice"
        assert _rejection_reason("Okay, let me check the cluster status") == "assistant_voice"

    def test_reason_names_the_failure_mode(self) -> None:
        assert _rejection_reason("<think>I need to validate timezone") == "markup"
        assert _rejection_reason("I'll validate the timezone configuration") == "assistant_voice"

    def test_length_alone_is_not_a_rejection(self) -> None:
        """#178 suggested treating an exactly-LABEL_MAX_CHARS output as a
        failure. 16 of the 287 real production labels are exactly that long —
        the rule was measured and dropped, and this pins that it stays dropped."""
        exactly_max = "DRIVE_PROBE_RESULT mounted=<yes|no> writable=<yes|no> path=<abso"
        assert len(exactly_max) == LABEL_MAX_CHARS
        assert _rejection_reason(exactly_max) is None

    def test_rejects_a_fenced_code_block_opener(self) -> None:
        """Replaying the three real preview prompts through the old default
        produced this: the model started answering in a code block and
        `_normalize` kept the fence line."""
        assert _rejection_reason("```json") == "markup"
        assert _rejection_reason("```") == "markup"

    def test_a_tag_after_the_first_character_is_not_a_rejection(self) -> None:
        """The markup rule is anchored: `writable=<yes|no>` is a real label
        shape, and a contains-a-tag-anywhere rule fired on 9 of the 287."""
        assert _rejection_reason("report writable=<yes|no> to the fleet") is None

    def test_rejects_an_echo_of_the_system_prompt(self) -> None:
        """Observed while measuring model candidates: the model repeated its own
        instruction instead of applying it, and `_normalize` truncated that to
        64 characters like any other output."""
        echoed = _LABEL_SYSTEM_PROMPT[:LABEL_MAX_CHARS]
        assert _rejection_reason(echoed) == "instruction_echo"

    def test_a_short_label_sharing_an_opening_word_survives(self) -> None:
        """The length floor: 'Summarize' is a prefix of the system prompt but is
        a plausible label, so only a long verbatim run counts as an echo."""
        assert _rejection_reason("Summarize") is None

    def test_a_label_echoing_the_user_prompt_is_not_rejected(self) -> None:
        """The input-side generalisation is deliberately absent — it rejects this
        real production label, which is a faithful summary of a short prompt that
        opens with exactly those words."""
        assert _rejection_reason("revert verification - terminating immediately") is None

    def test_words_that_merely_start_with_a_pronoun_survive(self) -> None:
        """`i`/`we` are matched as whole words — a label starting "Improve" or
        "Web" is not assistant voice."""
        assert _rejection_reason("Improve help discoverability and guidance") is None
        assert _rejection_reason("Web scraping for the pricing catalog") is None
        assert _rejection_reason("iOS build pipeline") is None
        assert _rejection_reason("okay") is None


class TestGenerateLabelRejectsNonLabels:
    """The behaviour that matters: a failed generation must not reach the
    database, and must take the daemon's existing failure path (return False ->
    `_dispatch_loop` records exponential backoff and retries) rather than
    landing as a label."""

    @pytest.mark.parametrize("raw", _ISSUE_178_OUTPUTS)
    @pytest.mark.asyncio
    async def test_issue_178_output_leaves_label_null(
        self,
        raw: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        _no_publish: list[str],
    ) -> None:
        tid = create_agent(db_conn)
        monkeypatch.setattr(labeler_module, "build_chat_model", lambda _m, **_: _FakeLLM(raw))  # pyright: ignore[reportUnknownArgumentType]

        result = await generate_label_async(tid, "a long agent brief", "deepseek-v4-flash")

        assert result is False, f"expected a generation failure for {raw!r}"
        assert _label_of(db_conn, tid) is None
        assert _no_publish == []

    @pytest.mark.asyncio
    async def test_good_label_still_written(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        _no_publish: list[str],
    ) -> None:
        """The other half of the contract: the classifier must not fire on a
        real label."""
        tid = create_agent(db_conn)
        monkeypatch.setattr(
            labeler_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLM(
                "\u9a8c\u8bc1\u9884\u89c8\u96c6\u7fa4\u65f6\u533a\u7edf\u4e00\u5e76\u6d4b\u8bd5SDK"
            ),  # pyright: ignore[reportUnknownArgumentType]
        )

        result = await generate_label_async(tid, "a long agent brief", "deepseek-v4-flash")

        assert result is True
        assert (
            _label_of(db_conn, tid)
            == "\u9a8c\u8bc1\u9884\u89c8\u96c6\u7fa4\u65f6\u533a\u7edf\u4e00\u5e76\u6d4b\u8bd5SDK"
        )
        assert len(_no_publish) == 1


@pytest.mark.asyncio
async def test_label_generation_logs_batch_usage_for_the_target_agent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    _no_publish: list[str],
    loguru_records: list[dict[str, Any]],
) -> None:
    """A labeler's daemon record must charge the label's agent, not the daemon."""
    agent_id = create_agent(db_conn)

    class _UsageLLM:
        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return AIMessage(
                content="Review deployment readiness",
                usage_metadata={
                    "input_tokens": 50,
                    "output_tokens": 8,
                    "total_tokens": 58,
                },
            )

    def _build_usage_llm(_model: str, **_kwargs: object) -> _UsageLLM:
        return _UsageLLM()

    monkeypatch.setattr(labeler_module, "build_chat_model", _build_usage_llm)

    assert await generate_label_async(agent_id, "a long agent brief", "deepseek-v4-flash") is True

    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["agent_id"] == agent_id
    assert record["extra"]["usage_kind"] == "batch"
