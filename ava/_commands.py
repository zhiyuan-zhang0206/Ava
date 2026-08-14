"""Composer commands — registered prompt templates + their expansion.

A *command* is a `commands/<name>.md` file: optional `description` +
`instruction-hint` frontmatter, body = a fixed prompt. Skills, plugins, the
repo, and the user overlay all contribute commands. A command takes exactly one
argument — the free-text natural-language instruction typed after `/name`; the
`instruction-hint` is the placeholder for it (Claude Code's `argument-hint` key
is also read, for imported CC-plugin commands).

This module is private (`_`) and is never registered on the `ava.*` namespace;
the expansion machinery is an implementation detail. The web Composer sends the
raw `/<name> <free text>` a human typed; `expand_command` rewrites it (here, in
the agent's claim node, before the message is wrapped for the model) into the
final prompt. The model only ever sees that expanded string. The gateway reuses
`discover_commands` to feed the `/`-autocomplete.

**One send is one message.** A caller may invoke several commands at once
(`/plan the migration /recap`), and the whole chain expands *inside that single
message* — in the order typed, each command keeping the free text that followed
it. Splitting the send into one message per command is what this deliberately
does not do: separate messages are claimed as separate turns, so the model
would answer each command in ignorance of the rest, with no ordering or
atomicity guarantee.

Every command is **the same kind of thing to this module** — a named prompt
template. Expansion never classifies them, never treats a combination as
special, and never refuses one. A chain is exactly the concatenation of what
each command expands to alone. Whether two prompts make sense together is a
question about the prompts, and the agent reading them answers it: `/compact`
tells the agent to replace its own context, so instructions after it lapse —
that is the prompt's meaning playing out, not a case for the mechanism to
predict, intercept, or warn about.

Commands are also sendable **agent-to-agent**: any chat inbound runs through
`expand_command` in the receiver's claim node (`agent/graph/_claim.py`), so
`ava.agents.send_message(peer, "/demo:brainstorming …")` (or
`spawn(prompt=…)`) delivers a *named, described* intent — a command is an
addressable prompt function, not just a human UI macro. Peers discover the
catalog via the read-only `ava.agents.commands()` (name + description +
instruction-hint, no body). Because of this dual audience, `expand_command` is
**source-neutral**: it never names the actor — `wrap_inbound` already attributes
the sender ("Agent 5:" / "User:").

Namespace: the composer identifier mirrors the skill namespace tree
(`ava/skills.py`) — bare for built-ins, `plugin:name` for a plugin's, `a:b:name`
for deeper folder nesting (`:`-joined, like `skills.identifier`). A skill-as-command's name is
`skills.identifier(skill)` and its `skill_target` is `skills.target(skill)`.

Discovery sources (later overrides earlier on name collision):
  0. every active skill, as `/[ns.]<skill>`      — skill-as-command (lowest)
  1. `<repo>/commands/<name>.md`                 — project (bare)
  2. `~/.ava/commands/<name>.md`                 — user (bare)
  3. `<repo>/ava_builtins/plugins/<p>/commands/<name>.md`     — built-in plugin → `/p.name`
  4. `~/.ava/plugins/<p>/commands/<name>.md`     — installed plugin → `/p.name`
  5. `<skill-dir>/commands/<name>.md`            — skill-carried (under the
                                                   carrying skill's namespace path)

Sources 5 and 0 walk the *active* skill set (`ava.skills._names()`), inheriting
its install-registry gating. Source 0 gives every skill a same-named
`/`-command for free; an explicit command file overrides it.

A skill-as-command expands *differently* from a prompt-template command: rather
than inlining a body, it tells the agent to load and follow the skill via
`ava.help` (see `expand_command`) — so the skill stays the single source of
truth, references and all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, TypedDict

from ava import skills
from shared.config import settings
from shared.frontmatter import FrontmatterError, parse_frontmatter
from shared.log import logger
from shared.paths import ava_home, plugins_dir, repo_plugins_dir, repo_root
from shared.skill_names import display_name, match_key


def _commands_enabled() -> bool:
    """Whether the `/<name>` command module is enabled — profile-safe.

    `commands_enabled` lives in the agent config domain, which the gateway
    process profile does not construct (per-process config, Task #856) — yet
    the gateway serves the Composer's `/`-autocomplete
    (`gateway/routers/commands.py` -> `discover_commands`) and needs the
    value. On a profile without the agent domain, read the cluster value from
    the .env FILE (the authoritative source; same fallback pattern as
    `shared/lm/factory.py` after the gateway pop) and default to True. On an
    agent process the ordinary `settings.agent.commands_enabled` read is used,
    unchanged.

    The `settings.has_domain` guard is the sanctioned cross-profile pattern:
    the consumption-matrix guard test treats a read under such a guard as
    legitimate (see tests/shared/test_gateway_consumer_guard.py).
    """
    if settings.has_domain("agent"):
        return settings.agent.commands_enabled
    from shared.runtime_config import read_env_aliases

    value = read_env_aliases().get("AVA_COMMANDS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Command(TypedDict):
    """One composer command. `name` is the `/`-invocation identifier — bare for
    built-ins, `:`-joined for namespaced ones (`plugin:item`, `a:b:item`).
    `description` and `instruction_hint` come from frontmatter (empty when
    absent); `body` is the prompt template (empty for a skill-as-command).
    `skill_target` is the `ava.skills.<…>` access path when this is a
    skill-as-command (e.g. `demo.brainstorming`), else None —
    `expand_command` branches on it.

    A command takes exactly one argument: the free-text natural-language
    instruction a caller types after `/name`. `instruction_hint` is the
    placeholder describing what that instruction should be — not a list of
    parsed args, so there is no delimiter/parsing."""

    name: str
    description: str
    instruction_hint: str
    body: str
    skill_target: str | None


def _command_dirs() -> list[tuple[Path, tuple[str, ...]]]:
    """`(commands_dir, base_namespace)` pairs in override order (see module
    docstring). `base_namespace` is `()` for repo / overlay, `(plugin,)` for a
    plugin's bundled dir, and the carrying skill's full namespace path for a
    skill-carried dir."""
    dirs: list[tuple[Path, tuple[str, ...]]] = [
        (repo_root() / "commands", ()),
        (ava_home() / "commands", ()),
    ]
    for base in (repo_plugins_dir(), plugins_dir()):
        if base.is_dir():
            dirs.extend(
                (p / "commands", (p.name,))
                for p in sorted(base.iterdir())
                if p.is_dir() and not p.name.startswith((".", "_"))
            )
    for sk in skills._names():
        dirs.append((Path(sk["path"]) / "commands", (*sk["namespace"], sk["name"])))
    return dirs


def _scan_dir(d: Path, base_ns: tuple[str, ...]) -> dict[str, Command]:
    """Scan `<d>/*.md` into `{name: Command}`, prefixing each name with the
    `:`-joined base namespace.

    A file without frontmatter is valid — the whole file is the prompt body.
    A file with an opener but malformed frontmatter is an authoring error;
    it is logged and skipped so one bad file can't break the whole picker.
    """
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
        # Canonical display spelling: dash for every segment (a plugin's
        # namespace is its directory name — `ava_code`, a Python package dir —
        # and the skill tree may hold legacy underscore folders), and `:`
        # between segments, exactly like skills.identifier() renders
        # skill-as-commands (`ava-code:pr`, `web-ai:deep-research`). Inbound
        # matching still folds either spelling (split_commands -> match_key),
        # so nothing breaks.
        name = ":".join(display_name(seg) for seg in (*base_ns, f.stem))
        out[name] = {
            "name": name,
            "description": fields.get("description", ""),
            # Accept Claude Code's `argument-hint` key too, so commands imported
            # from a CC plugin keep their hint (vendor interop, not back-compat).
            "instruction_hint": fields.get("instruction-hint") or fields.get("argument-hint", ""),
            "body": body.strip(),
            "skill_target": None,
        }
    return out


def _skill_commands() -> dict[str, Command]:
    """Every active skill as a same-named command (source 0, lowest priority).

    The command name is the skill's `:`-identifier (e.g. `demo:brainstorming`);
    `skill_target` is its `ava.skills` access path, so `expand_command` rewrites
    it into a load-and-follow instruction rather than inlining a body.
    """
    out: dict[str, Command] = {}
    for sk in skills._names():
        ident = skills.identifier(sk)
        out[ident] = {
            "name": ident,
            "description": sk["description"],
            "instruction_hint": "",
            "body": "",
            "skill_target": skills.target(sk),
        }
    return out


def discover_commands() -> list[Command]:
    """All composer commands across every source, deduped by name (later
    source wins) and sorted by name. Skill-as-commands (source 0) seed the map
    first, so an explicit command file overrides a same-named skill.

    Empty when the command module is disabled (settings.agent.commands_enabled)."""
    if not _commands_enabled():
        return []
    out: dict[str, Command] = _skill_commands()
    for d, base_ns in _command_dirs():
        out.update(_scan_dir(d, base_ns))
    return [out[name] for name in sorted(out)]


class Invocation(NamedTuple):
    """One command inside a message: the resolved `command`, the `typed_name`
    exactly as spelled (`demo.brainstorming` and `demo:brainstorming` resolve to
    the same command but each expands under the spelling used), and the
    free-text `argument` that followed it up to the next command."""

    command: Command
    typed_name: str
    argument: str


# A command starts at the very beginning of the message or after whitespace.
# `\S+` takes the whole token, so `/compact/update` is one (unknown) name
# rather than two commands — a missing space is a typo, not a chain.
_INVOCATION = re.compile(r"(?:\A|(?<=\s))/(\S+)")


def split_commands(content: str) -> list[Invocation] | None:
    """Split a raw `/`-message into the chain of commands it invokes.

    A message enters command parsing only when it *starts* with a known command
    — the long-standing precondition, now applied to the head of a chain. From
    there, each later `/token` that resolves to a registered command opens the
    next segment; a slash that is not a known command name stays inside the
    preceding command's free text, so `/recap check /path/to/file` is one
    command whose argument holds a path, not two commands.

    Returns None when the message does not begin with a known command, which
    tells the caller to pass the text through untouched.
    """
    # Match through the dash/underscore/colon fold so every spelling of a
    # command name resolves (`/demo.brainstorming` and `/demo:brainstorming`
    # both hit the `demo:brainstorming` command).
    catalog = {match_key(c["name"]): c for c in discover_commands()}
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


def _expand_one(inv: Invocation) -> str:
    """Expand a single invocation into its prompt text.

    Source-neutral: the envelope (shared.envelope.wrap_inbound) already
    attributes the sender ("Agent 5:" / "User:"), so the expansion must not
    re-name an actor — a `/command` sent by a peer agent reads correctly
    without the old "User invoked …" phrasing.
    """
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


def expand_command(content: str) -> str:
    """Rewrite a raw `/<name> <free text>` composer message into the prompt the
    model sees.

    One message may invoke several commands (`/plan the migration /recap`).
    Each is expanded exactly as it would be alone — in the order typed, with
    the free text that followed it — and the results are concatenated into this
    one message. No command is treated as special and no combination is
    refused: what two prompts mean together is for the agent reading them to
    work out.

    Leaves the text untouched when the command module is disabled
    (settings.agent.commands_enabled), when it isn't a `/`-command, or when the
    leading name doesn't match a known command (so a stray leading slash is
    harmless).
    """
    if not _commands_enabled() or not content.startswith("/"):
        return content
    chain = split_commands(content)
    if chain is None:
        return content
    return "\n\n".join(_expand_one(inv) for inv in chain)
