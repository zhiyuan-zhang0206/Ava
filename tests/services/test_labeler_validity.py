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
    "仅作为总指挥",
    "前端Markdown渲染横向滚动失效问题",
    "功能规划与待办梳理",
    "实现agent活动追踪、push、PR与CI监控",
    "在 Sidebar 中添加显示已停止 agents 的切换开关",
    "请自行处理preview集群测试",
    "Agent消息架构表格渲染异常排查",
    "Google Drive用户行为数据分析与视频理解",
    "音频转录",
    "审查PR #54并执行自动代码审查",
    "下载Claude配置保存到本地",
    "读取并验证声明后写入JSON",
    "将JSON写入文件并终止",
    "审计 SWE-bench runner 改为流式输出的代码改动点",
    "创建AI agent深度对比表并产出HTML页面",
    "修复serve_markdown模板缺失",
    "为enum增加Jina Reader升级以适配人民网抓取",
    "清理config_overlay中skills_to_inject_into_system_prompt的旧格式skill名",
    "调查集群拓扑与单机多cluster问题",
    "代码层全量重命名原生辅助工具为ava-desktop-helper",
    "修复pg_tools._free_port的TOCTOU竞态问题",
    "钉住chrome-devtools-mcp版本并审计未钉依赖",
    "调查消息投递延迟与轮询机制审计",
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
    "spawn报错agent起不来",
    "诊断spawn报错agent无法启动",
    "labeler 为什么给 agent 起怪名字",
    "询问 labeler 命名 agent 的原因",
    "标签器如何决定智能体显示名",
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
        monkeypatch.setattr(labeler_module, "build_chat_model", lambda _m, **_: _FakeLLM(raw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

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
            lambda _m, **_: _FakeLLM("验证预览集群时区统一并测试SDK"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )

        result = await generate_label_async(tid, "a long agent brief", "deepseek-v4-flash")

        assert result is True
        assert _label_of(db_conn, tid) == "验证预览集群时区统一并测试SDK"
        assert len(_no_publish) == 1
