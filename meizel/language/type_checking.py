"""Rule: no `if TYPE_CHECKING:` import folding, except for the files listed here."""

from __future__ import annotations

import ast

__all__ = ["violations"]

# Files allowed to use `if TYPE_CHECKING:` — real circular import or heavy
# optional dependency. Add an entry with a one-line reason.
_TYPE_CHECKING_ALLOWED: frozenset[str] = frozenset(
    {
        # PEP 562 lazy re-export: the node set is a heavy import on paths (the
        # exec child) that never use the names; TYPE_CHECKING restores the real
        # signatures for static checkers without putting them back on the import
        # path (task #3585).
        "agent/graph/__init__.py",
        # The exec child builds an `AvaContext` from its request envelope: the handle types
        # (psycopg pool, redis bus, chat model) are annotation-only fields it never holds, and
        # importing them would put the whole DB / LM stack on its boot path.
        "base/agents/context/__init__.py",
        # The same exec-child boot path: a ClientSet builds psycopg / redis / httpx clients on first
        # use only, so its client types are annotation-only.
        "base/agents/context/clients.py",
        # LM provider registration surface: the chat-model stack is a heavy
        # import on the exec-child boot path, which never uses the type
        # (annotation-only references; task #3633).
        "base/lm/catalog/provider_contract.py",
        "base/lm/factory.py",
        "ava_builtins/plugins/lm_alibaba/provider.py",
        "ava_builtins/plugins/lm_anthropic/provider.py",
        "ava_builtins/plugins/lm_deepseek/provider.py",
        "ava_builtins/plugins/lm_google/provider.py",
        "ava_builtins/plugins/lm_moonshot/provider.py",
        "ava_builtins/plugins/lm_openai/provider.py",
        "ava_builtins/plugins/lm_xiaomi/provider.py",
        "ava_builtins/plugins/lm_zhipu/provider.py",
        # LangChain message-stack trim on the same registration path: message
        # types appear in annotations only, or (pricing) import at the call
        # site of a runtime isinstance (task #3633).
        "base/lm/stop.py",
        "base/lm/pricing/__init__.py",
        "base/agents/messages/kwargs.py",
        "base/lm/content.py",
        # Exec-child boot path (`_run_code` -> sdk_telemetry): ToolMessage is
        # runtime-only (imported at the isinstance call site), BaseMessage
        # annotation-only (task #3633).
        "base/agents/sdk/telemetry.py",
        # Boot-lite facade: the names are served at runtime by the lite latch;
        # TYPE_CHECKING keeps `from base.config import X` consumers resolving
        # without an eager import that would rebuild the config chain the lite
        # facade defers (task #3621).
        "base/config/__init__.py",
        # Boot-path trim: the skill-index stack is a heavy import every exec
        # child pays; `SkillFile` is annotation-only on the mount helpers
        # (script/module-level imports removed for task #3816).
        "ava/skills/__init__.py",
        # Boot-path trim: the fleet plugin (autoloaded into every exec child)
        # keeps psycopg off its module import graph — `psycopg` is
        # annotation-only here, imported at the raise sites (task #3816).
        "ava_builtins/plugins/ava_fleet/task_registry.py",
        "ava_builtins/plugins/ava_fleet/_task_update.py",
        "base/agents/tasks/reparent.py",
        "base/agents/tasks/rules.py",
    }
)


def _lines(tree: ast.Module) -> list[int]:
    """Line numbers of `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` blocks."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = (
            test.id
            if isinstance(test, ast.Name)
            else test.attr
            if isinstance(test, ast.Attribute)
            else None
        )
        if name == "TYPE_CHECKING":
            hits.append(node.lineno)
    return hits


def violations(tree: ast.Module, rel_path: str) -> list[tuple[int, str]]:
    """`(line, message)` for every unlisted `if TYPE_CHECKING:` block of a governed module."""
    if rel_path in _TYPE_CHECKING_ALLOWED:
        return []
    return [
        (
            lineno,
            "`if TYPE_CHECKING:` is banned — import at top level. "
            "App deps are always on the runtime path and LangGraph/Pydantic "
            "introspect type hints at runtime (deferred imports NameError). "
            "Real circular-import / heavy-dep cases: refactor, or add this file "
            "to _TYPE_CHECKING_ALLOWED in meizel/language/type_checking.py with a reason.",
        )
        for lineno in _lines(tree)
    ]
