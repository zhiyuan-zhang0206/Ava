"""Composer command discovery + expansion (`base.packages.composer_commands`)."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from base.packages import composer_commands
from base.packages.composer_commands import CommandCatalog
from base.packages.skills.tree import Skill


def _dirs(
    *dirs: tuple[Path, tuple[str, ...]],
) -> Callable[[Sequence[Skill]], list[tuple[Path, tuple[str, ...]]]]:
    """A `command_dirs` that lists exactly these directories whatever the skills are."""

    def command_dirs(_skills: Sequence[Skill]) -> list[tuple[Path, tuple[str, ...]]]:
        return list(dirs)

    return command_dirs


def _write(d: Path, name: str, text: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_scan_dir_parses_frontmatter(tmp_path: Path):
    _write(
        tmp_path,
        "recap.md",
        "---\ndescription: recap it\ninstruction-hint: a focus\n---\n\nDo the recap.\n",
    )
    assert composer_commands.scan_dir(tmp_path, ())["recap"] == {
        "name": "recap",
        "description": "recap it",
        "instruction_hint": "a focus",
        "body": "Do the recap.",
        "skill_target": None,
    }


def test_scan_dir_reads_claude_code_argument_hint(tmp_path: Path):
    # CC plugins ship `argument-hint`; we read it into instruction_hint (interop).
    _write(tmp_path, "deploy.md", "---\nargument-hint: env\n---\nDeploy.\n")
    assert composer_commands.scan_dir(tmp_path, ())["deploy"]["instruction_hint"] == "env"


def test_scan_dir_no_frontmatter_uses_whole_body(tmp_path: Path):
    _write(tmp_path, "plain.md", "just a prompt, no frontmatter")
    out = composer_commands.scan_dir(tmp_path, ())["plain"]
    assert out["body"] == "just a prompt, no frontmatter"
    assert out["description"] == ""
    assert out["instruction_hint"] == ""


def test_scan_dir_skips_malformed_frontmatter(tmp_path: Path):
    _write(tmp_path, "bad.md", "---\ndescription: oops\nno terminator\n")
    _write(tmp_path, "good.md", "fine")
    out = composer_commands.scan_dir(tmp_path, ())
    assert "bad" not in out
    assert "good" in out


def test_scan_dir_missing_dir_is_empty(tmp_path: Path):
    assert composer_commands.scan_dir(tmp_path / "nope", ()) == {}


def test_scan_dir_namespace_prefixes_name(tmp_path: Path):
    _write(tmp_path, "deploy.md", "ship it")
    out = composer_commands.scan_dir(tmp_path, ("vercel",))
    assert "vercel:deploy" in out
    assert out["vercel:deploy"]["name"] == "vercel:deploy"


def test_discover_dedups_later_source_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    early, late = tmp_path / "early", tmp_path / "late"
    _write(early, "dup.md", "early body")
    _write(late, "dup.md", "late body")
    monkeypatch.setattr(composer_commands, "command_dirs", _dirs((early, ()), (late, ())))  # pyright: ignore[reportUnknownArgumentType]
    out = {c["name"]: c for c in composer_commands.discover_commands([])}
    assert out["dup"]["body"] == "late body"


def test_discover_sorted_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path, "zebra.md", "z")
    _write(tmp_path, "alpha.md", "a")
    monkeypatch.setattr(composer_commands, "command_dirs", _dirs((tmp_path, ())))
    assert [c["name"] for c in composer_commands.discover_commands([])] == ["alpha", "zebra"]


def test_builtin_commands_discoverable():
    # End-to-end (no monkeypatch): ava_builtins/commands (recap, compact) stay bare.
    names = {c["name"] for c in composer_commands.discover_commands([])}
    assert {"recap", "compact"} <= names
    assert "plan" not in names


def test_removed_builtin_plan_passes_through():
    commands = composer_commands.discover_commands([])
    assert composer_commands.expand_command("/plan the migration", commands) == (
        "/plan the migration"
    )


def test_builtin_source_ignores_legacy_root_and_keeps_user_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, home = tmp_path / "repo", tmp_path / "home"
    _write(repo / "ava_builtins" / "commands", "recap.md", "Builtin recap.")
    _write(repo / "commands", "plan.md", "Old bundled plan.")
    _write(home / "commands", "recap.md", "User recap.")
    monkeypatch.setattr(composer_commands, "repo_root", lambda: repo)
    monkeypatch.setattr(composer_commands, "ava_home", lambda: home)
    monkeypatch.setattr(composer_commands, "repo_plugins_dir", lambda: repo / "plugins")
    monkeypatch.setattr(composer_commands, "external_plugin_read_root", lambda: home / "plugins")
    catalog = composer_commands.discover_commands([])
    commands = {c["name"]: c for c in catalog}
    assert set(commands) == {"recap"}
    assert commands["recap"]["body"] == "User recap."
    assert composer_commands.expand_command("/plan the migration", catalog) == (
        "/plan the migration"
    )


def test_scan_dir_renders_canonical_namespace_spelling(tmp_path: Path):
    # An underscore plugin / skill folder must present the dash form outward,
    # `:`-joined like a skill identifier.
    _write(tmp_path, "deploy.md", "ship it")
    out = composer_commands.scan_dir(tmp_path, ("ava_fleet",))
    assert "ava-fleet:deploy" in out
    assert out["ava-fleet:deploy"]["name"] == "ava-fleet:deploy"


def test_underscore_command_spelling_still_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The picker shows the canonical `:`-joined dash name; inbound matching
    # folds dash/underscore + colon/dot, so a legacy `/ava_fleet.ship` still
    # expands the same command as `/ava-fleet:ship`.
    _write(tmp_path, "ship.md", "write the release note")
    monkeypatch.setattr(
        composer_commands,
        "command_dirs",
        _dirs((tmp_path, ("ava_fleet",))),
    )
    catalog = CommandCatalog(skills=list)
    dash = catalog.expand("/ava-fleet:ship fix the tests")
    legacy = catalog.expand("/ava_fleet.ship fix the tests")
    # Both spellings resolve to the same command (same body); the expansion
    # echoes the spelling the user typed, so only the echoed name differs.
    assert "write the release note" in dash
    assert "write the release note" in legacy
    assert "fix the tests" in dash and "fix the tests" in legacy


def test_expand_passes_through_non_command():
    assert composer_commands.expand_command("just chatting", []) == "just chatting"
    assert composer_commands.expand_command("/", []) == "/"


def test_expand_passes_through_unknown_command():
    assert composer_commands.expand_command("/nope do x", []) == "/nope do x"


def _file_command(name: str, body: str) -> composer_commands.Command:
    return {
        "name": name,
        "description": "",
        "instruction_hint": "",
        "body": body,
        "skill_target": None,
    }


def test_expand_known_command_without_arg():
    commands = [_file_command("recap", "Recap it.")]
    assert composer_commands.expand_command("/recap", commands) == "Command /recap:\nRecap it."


def test_expand_known_command_with_arg():
    commands = [_file_command("recap", "Recap it.")]
    assert composer_commands.expand_command("/recap just the PRs", commands) == (
        "Command /recap:\nRecap it.\nAdditional message: just the PRs"
    )


# --- skill-as-command (source 0) ---


def _fake_skill(name: str, namespace: tuple[str, ...] = ()) -> Skill:
    return {"name": name, "description": f"{name} skill", "path": "/x", "namespace": namespace}


def _skill_catalog(*skills: Skill) -> list[composer_commands.Command]:
    """The commands the given skills alone provide (no command files)."""
    return list(composer_commands.skill_commands(skills).values())


def test_skill_as_command_bare():
    out = {c["name"]: c for c in _skill_catalog(_fake_skill("goal"))}
    assert out["goal"]["skill_target"] == "goal"
    assert "ava.help(ava.skills.goal)" in composer_commands.expand_command(
        "/goal", _skill_catalog(_fake_skill("goal"))
    )


def test_skill_as_command_namespaced():
    commands = _skill_catalog(_fake_skill("brainstorming", ("demo",)))
    out = {c["name"]: c for c in commands}
    assert "demo:brainstorming" in out
    assert out["demo:brainstorming"]["skill_target"] == "demo.brainstorming"
    expanded = composer_commands.expand_command("/demo:brainstorming a login form", commands)
    assert "ava.help(ava.skills.demo.brainstorming)" in expanded
    assert expanded.endswith("Additional message: a login form")


def test_skill_as_command_dotted_input_still_resolves():
    """Backcompat: the pre-colon dotted spelling of a command name still
    expands — matching folds dash/underscore/colon."""
    commands = _skill_catalog(_fake_skill("brainstorming", ("demo",)))
    expanded = composer_commands.expand_command("/demo.brainstorming a login form", commands)
    assert "ava.help(ava.skills.demo.brainstorming)" in expanded


def test_skill_as_command_hyphen_maps_to_underscore_target():
    commands = _skill_catalog(_fake_skill("test-driven-development", ("demo",)))
    out = composer_commands.expand_command("/demo.test-driven-development", commands)
    assert "ava.help(ava.skills.demo.test_driven_development)" in out


def test_explicit_command_overrides_skill_as_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # a skill 'foo' AND an explicit foo.md — the file (source 1-5) wins over source 0
    _write(tmp_path, "foo.md", "explicit prompt body")
    monkeypatch.setattr(composer_commands, "command_dirs", _dirs((tmp_path, ())))
    out = {c["name"]: c for c in composer_commands.discover_commands([_fake_skill("foo")])}
    assert out["foo"]["skill_target"] is None
    assert out["foo"]["body"] == "explicit prompt body"


def test_skill_carried_commands_walk_the_skills_it_is_given():
    """Source 5: each skill's own `commands/` dir is read under that skill's namespace path."""
    skills = [_fake_skill("brainstorming", ("demo",)), _fake_skill("goal")]
    assert composer_commands.command_dirs(skills)[-2:] == [
        (Path("/x/commands"), ("demo", "brainstorming")),
        (Path("/x/commands"), ("goal",)),
    ]


# --- commands_enabled gate ---


def test_discover_empty_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path, "recap.md", "Recap it.")
    monkeypatch.setattr(composer_commands, "command_dirs", _dirs((tmp_path, ())))
    monkeypatch.setattr(composer_commands.settings.agent, "commands_enabled", False)
    assert composer_commands.discover_commands([_fake_skill("goal")]) == []


def test_expand_passthrough_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path, "recap.md", "Recap it.")
    monkeypatch.setattr(composer_commands, "command_dirs", _dirs((tmp_path, ())))
    catalog = CommandCatalog(skills=list)
    assert catalog.expand("/recap just the PRs").startswith("Command /recap:")
    monkeypatch.setattr(composer_commands.settings.agent, "commands_enabled", False)
    assert catalog.expand("/recap just the PRs") == "/recap just the PRs"


def test_the_catalog_reads_its_skill_source_on_every_call():
    installed: list[Skill] = []
    catalog = CommandCatalog(skills=lambda: installed)

    def skill_commands() -> list[str]:
        return [c["name"] for c in catalog.discover() if c["skill_target"] is not None]

    assert skill_commands() == []
    installed.append(_fake_skill("goal"))
    assert skill_commands() == ["goal"]
    assert catalog.expand("/goal now").endswith("Additional message: now")


# --- multi-command chains (one message, several commands) ---


def test_chain_expands_every_command_in_the_order_typed():
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    out = composer_commands.expand_command("/recap /review", commands)
    assert out.index("Command /recap:\nRecap it.") < out.index("Command /review:\nReview it.")
    # Reversing the input reverses the expansion — order is the typed order.
    flipped = composer_commands.expand_command("/review /recap", commands)
    assert flipped.index("Command /review:") < flipped.index("Command /recap:")


def test_chain_gives_each_command_its_own_argument():
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    out = composer_commands.expand_command("/recap just the PRs /review the migration", commands)
    assert "Command /recap:\nRecap it.\nAdditional message: just the PRs" in out
    assert "Command /review:\nReview it.\nAdditional message: the migration" in out


def test_chain_is_exactly_the_single_expansions_concatenated():
    # The mechanism adds nothing of its own around a chain — no preamble, no
    # framing, no instruction about how to treat the combination. A chain is
    # what each command expands to alone, joined by a blank line.
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    alone = [
        composer_commands.expand_command("/recap", commands),
        composer_commands.expand_command("/review the migration", commands),
    ]
    assert composer_commands.expand_command("/recap /review the migration", commands) == (
        "\n\n".join(alone)
    )


def test_single_command_expands_unchanged():
    commands = [_file_command("recap", "Recap it.")]
    assert composer_commands.expand_command("/recap", commands) == "Command /recap:\nRecap it."


def test_chain_mixes_file_command_and_skill_as_command():
    commands = [_file_command("recap", "Recap it."), *_skill_catalog(_fake_skill("goal"))]
    out = composer_commands.expand_command("/recap the week /goal", commands)
    assert "Command /recap:\nRecap it.\nAdditional message: the week" in out
    assert "ava.help(ava.skills.goal)" in out


def test_slash_token_that_is_not_a_command_stays_in_the_argument():
    # A path in the free text is not a command boundary — only a registered
    # name opens the next segment.
    commands = [_file_command("recap", "Recap it.")]
    assert composer_commands.expand_command("/recap check /path/to/file", commands) == (
        "Command /recap:\nRecap it.\nAdditional message: check /path/to/file"
    )


def test_missing_space_between_commands_is_one_unknown_name():
    # `/recap/review` is a typo, not a chain: the whole token is the name, it
    # matches nothing, and the message passes through untouched.
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    assert composer_commands.expand_command("/recap/review", commands) == "/recap/review"


def test_extra_whitespace_between_commands_still_chains():
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    for raw in ("/recap    /review", "/recap\n/review"):
        out = composer_commands.expand_command(raw, commands)
        assert "Command /recap:\nRecap it." in out
        assert "Command /review:\nReview it." in out
        assert "Additional message" not in out


def test_text_not_starting_with_a_command_never_chains():
    commands = [_file_command("recap", "Recap it.")]
    # A command name mid-sentence is prose, not an invocation.
    assert (
        composer_commands.expand_command("check /recap output", commands) == "check /recap output"
    )


def test_unknown_leading_name_passes_the_whole_chain_through():
    commands = [_file_command("recap", "Recap it.")]
    assert composer_commands.expand_command("/nope /recap", commands) == "/nope /recap"


def test_chain_expansion_is_source_neutral():
    commands = [_file_command("recap", "Recap it."), _file_command("review", "Review it.")]
    assert "User" not in composer_commands.expand_command("/recap /review", commands)


# --- every command expands alike (no special cases) ---


def test_lifecycle_command_chains_like_any_other():
    """`/compact` tells the agent to replace its own context, which is the most
    disruptive thing a shipped command body says — and the expansion still
    treats it as an ordinary prompt template. No classification, no refusal, no
    warning: whether the instruction after it still applies is the agent's
    reading of the prompts, not a question the mechanism answers."""
    commands = [_file_command("compact", "Wind down."), _file_command("recap", "Recap it.")]
    for raw in ("/compact /recap", "/recap /compact"):
        out = composer_commands.expand_command(raw, commands)
        assert "Command /compact:\nWind down." in out
        assert "Command /recap:\nRecap it." in out
    # Order is still just the typed order — nothing gets hoisted or dropped.
    expanded = composer_commands.expand_command("/recap /compact", commands)
    assert expanded.index("Command /recap:") < expanded.index("Command /compact:")


def test_repeating_one_command_expands_it_twice():
    commands = [_file_command("compact", "Wind down.")]
    assert composer_commands.expand_command("/compact /compact", commands) == (
        "Command /compact:\nWind down.\n\nCommand /compact:\nWind down."
    )


def test_command_record_carries_no_chaining_policy():
    """The Command record has nowhere to put a per-command chaining rule, so the
    uniformity is structural rather than a convention the shipped files happen
    to follow. `skill_target` does branch the expansion, but it says what the
    command *is* (a skill rather than a template) and applies the same way to
    every skill-as-command — it is not a policy about which commands may share a
    message. A new field here means asking whether it is reintroducing one."""
    fields = set(composer_commands.Command.__annotations__)
    assert fields == {"name", "description", "instruction_hint", "body", "skill_target"}


# --- source-neutral expansion (agent-to-agent commands) ---


def test_expand_is_source_neutral():
    # Expansion must not name an actor — the envelope (wrap_inbound) frames the
    # message, so a /command sent by a peer agent reads correctly.
    commands = [_file_command("recap", "Recap it."), *_skill_catalog(_fake_skill("goal"))]
    for raw in ("/recap context", "/goal context"):
        assert "User" not in composer_commands.expand_command(raw, commands)
