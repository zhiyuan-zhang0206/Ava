from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

from base.config import settings
from base.host.runtime_interpreter import external_plugin_read_root
from base.log import logger
from base.packages.docs.frontmatter import FrontmatterError, parse_frontmatter
from base.packages.skills.names import display_name, match_key
from base.packages.skills.tree import Skill, identifier, target
from base.paths import ava_home, repo_plugins_dir, repo_root


# ava:ref base/packages/docs/composer-commands.ava.okf.md#catalog-lazy-boundary
def _commands_enabled() -> bool:
    if settings.has_domain("agent"):
        return settings.agent.commands_enabled
    from base.host.env.dotenv_file import read_env_aliases

    value = read_env_aliases().get("AVA_COMMANDS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-expansion
class Command(TypedDict):
    name: str
    description: str
    instruction_hint: str
    body: str
    skill_target: str | None


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-discovery
def command_dirs(skills: Sequence[Skill]) -> list[tuple[Path, tuple[str, ...]]]:
    dirs: list[tuple[Path, tuple[str, ...]]] = [
        (repo_root() / "ava_builtins" / "commands", ()),
        (ava_home() / "commands", ()),
    ]
    for base in (repo_plugins_dir(), external_plugin_read_root()):
        if base.is_dir():
            dirs.extend(
                (p / "commands", (p.name,))
                for p in sorted(base.iterdir())
                if p.is_dir() and not p.name.startswith((".", "_"))
            )
    for sk in skills:
        dirs.append((Path(sk["path"]) / "commands", (*sk["namespace"], sk["name"])))
    return dirs


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-discovery
def scan_dir(d: Path, base_ns: tuple[str, ...]) -> dict[str, Command]:
    out: dict[str, Command] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        if content.startswith("---\n"):
            try:
                fields, body = parse_frontmatter(content)
            except FrontmatterError as e:
                logger.warning("[commands] skip malformed {f}: {e}", f=f, e=e)
                continue
        else:
            fields, body = {}, content

        name = ":".join(display_name(seg) for seg in (*base_ns, f.stem))
        out[name] = {
            "name": name,
            "description": fields.get("description", ""),
            "instruction_hint": fields.get("instruction-hint") or fields.get("argument-hint", ""),
            "body": body.strip(),
            "skill_target": None,
        }
    return out


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-discovery
def skill_commands(skills: Sequence[Skill]) -> dict[str, Command]:
    out: dict[str, Command] = {}
    for sk in skills:
        ident = identifier(sk)
        out[ident] = {
            "name": ident,
            "description": sk["description"],
            "instruction_hint": "",
            "body": "",
            "skill_target": target(sk),
        }
    return out


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-discovery
def discover_commands(skills: Sequence[Skill]) -> list[Command]:
    if not _commands_enabled():
        return []
    out: dict[str, Command] = skill_commands(skills)
    for d, base_ns in command_dirs(skills):
        out.update(scan_dir(d, base_ns))
    return [out[name] for name in sorted(out)]


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-expansion
class Invocation(NamedTuple):
    command: Command
    typed_name: str
    argument: str


_INVOCATION = re.compile(r"(?:\A|(?<=\s))/(\S+)")


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-expansion
def split_commands(content: str, commands: Sequence[Command]) -> list[Invocation] | None:
    catalog = {match_key(c["name"]): c for c in commands}
    hits = [
        (m.start(), m.group(1), catalog[match_key(m.group(1))])
        for m in _INVOCATION.finditer(content)
        if match_key(m.group(1)) in catalog
    ]
    if not hits or hits[0][0] != 0:
        return None
    chain: list[Invocation] = []
    for i, (start, typed, cmd) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(content)
        chain.append(Invocation(cmd, typed, content[start + 1 + len(typed) : end].strip()))
    return chain


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-expansion
def _expand_one(inv: Invocation) -> str:
    name = inv.typed_name
    if inv.command["skill_target"]:
        out = (
            f"/{name} invokes the {name} skill. Load it with "
            f"ava.help(ava.skills.{inv.command['skill_target']}) and follow it for this task."
        )
    else:
        out = f"Command /{name}:\n{inv.command['body']}"
    if inv.argument:
        out += f"\nAdditional message: {inv.argument}"
    return out


# ava:ref base/packages/docs/composer-commands.ava.okf.md#command-expansion
def expand_command(content: str, commands: Sequence[Command]) -> str:
    if not content.startswith("/"):
        return content
    chain = split_commands(content, commands)
    if chain is None:
        return content
    return "\n\n".join(_expand_one(inv) for inv in chain)


# ava:ref base/packages/docs/composer-commands.ava.okf.md#catalog-lazy-boundary
@dataclass(frozen=True)
class CommandCatalog:
    skills: Callable[[], Sequence[Skill]]

    def discover(self) -> list[Command]:
        if not _commands_enabled():
            return []
        return discover_commands(self.skills())

    def expand(self, content: str) -> str:
        if not content.startswith("/"):
            return content
        return expand_command(content, self.discover())
