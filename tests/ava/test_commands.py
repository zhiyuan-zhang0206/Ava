"""Composer command discovery + expansion (`ava._commands`)."""

from pathlib import Path

import pytest

from ava import _commands


def _write(d: Path, name: str, text: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_scan_dir_parses_frontmatter(tmp_path: Path):
    _write(
        tmp_path,
        "recap.md",
        "---\ndescription: recap it\ninstruction-hint: a focus\n---\n\nDo the recap.\n",
    )
    assert _commands._scan_dir(tmp_path, ())["recap"] == {
        "name": "recap",
        "description": "recap it",
        "instruction_hint": "a focus",
        "body": "Do the recap.",
        "skill_target": None,
    }


def test_scan_dir_reads_claude_code_argument_hint(tmp_path: Path):
    # CC plugins ship `argument-hint`; we read it into instruction_hint (interop).
    _write(tmp_path, "deploy.md", "---\nargument-hint: env\n---\nDeploy.\n")
    assert _commands._scan_dir(tmp_path, ())["deploy"]["instruction_hint"] == "env"


def test_scan_dir_no_frontmatter_uses_whole_body(tmp_path: Path):
    _write(tmp_path, "plain.md", "just a prompt, no frontmatter")
    out = _commands._scan_dir(tmp_path, ())["plain"]
    assert out["body"] == "just a prompt, no frontmatter"
    assert out["description"] == ""
    assert out["instruction_hint"] == ""


def test_scan_dir_skips_malformed_frontmatter(tmp_path: Path):
    _write(tmp_path, "bad.md", "---\ndescription: oops\nno terminator\n")
    _write(tmp_path, "good.md", "fine")
    out = _commands._scan_dir(tmp_path, ())
    assert "bad" not in out
    assert "good" in out


def test_scan_dir_missing_dir_is_empty(tmp_path: Path):
    assert _commands._scan_dir(tmp_path / "nope", ()) == {}


def test_scan_dir_namespace_prefixes_name(tmp_path: Path):
    _write(tmp_path, "deploy.md", "ship it")
    out = _commands._scan_dir(tmp_path, ("vercel",))
    assert "vercel:deploy" in out
    assert out["vercel:deploy"]["name"] == "vercel:deploy"


def test_discover_dedups_later_source_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    early, late = tmp_path / "early", tmp_path / "late"
    _write(early, "dup.md", "early body")
    _write(late, "dup.md", "late body")
    monkeypatch.setattr(_commands, "_command_dirs", lambda: [(early, ()), (late, ())])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_commands, "_skill_commands", dict)
    out = {c["name"]: c for c in _commands.discover_commands()}
    assert out["dup"]["body"] == "late body"


def test_discover_sorted_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path, "zebra.md", "z")
    _write(tmp_path, "alpha.md", "a")
    monkeypatch.setattr(_commands, "_command_dirs", lambda: [(tmp_path, ())])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_commands, "_skill_commands", dict)
    assert [c["name"] for c in _commands.discover_commands()] == ["alpha", "zebra"]


def test_builtin_commands_discoverable():
    # End-to-end (no monkeypatch): repo-level commands/ (recap, plan) stay bare;
    # the ava_code plugin's commands/ are namespaced under the plugin folder.
    # The namespace renders in canonical display spelling — the plugin dir is a
    # Python package (`ava_code`), so the picker must show `ava-code.pr`, not
    # the raw directory name.
    names = {c["name"] for c in _commands.discover_commands()}
    assert {"recap", "plan"} <= names
    assert {"ava-code:pr", "ava-code:review", "ava-code:worktree"} <= names


def test_scan_dir_renders_canonical_namespace_spelling(tmp_path: Path):
    # An underscore plugin / skill folder must present the dash form outward,
    # `:`-joined like a skill identifier.
    _write(tmp_path, "deploy.md", "ship it")
    out = _commands._scan_dir(tmp_path, ("ava_code",))
    assert "ava-code:deploy" in out
    assert out["ava-code:deploy"]["name"] == "ava-code:deploy"


def test_underscore_command_spelling_still_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The picker shows the canonical `:`-joined dash name; inbound matching
    # folds dash/underscore + colon/dot, so a legacy `/ava_code.pr` still
    # expands the same command as `/ava-code:pr`.
    _write(tmp_path, "pr.md", "write the PR body")
    monkeypatch.setattr(
        _commands,
        "_command_dirs",
        lambda: [(tmp_path, ("ava_code",))],
    )
    monkeypatch.setattr(_commands, "_skill_commands", dict)
    dash = _commands.expand_command("/ava-code:pr fix the tests")
    legacy = _commands.expand_command("/ava_code.pr fix the tests")
    # Both spellings resolve to the same command (same body); the expansion
    # echoes the spelling the user typed, so only the echoed name differs.
    assert "write the PR body" in dash
    assert "write the PR body" in legacy
    assert "fix the tests" in dash and "fix the tests" in legacy


def test_expand_passes_through_non_command():
    assert _commands.expand_command("just chatting") == "just chatting"
    assert _commands.expand_command("/") == "/"


def test_expand_passes_through_unknown_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_commands, "discover_commands", list)
    assert _commands.expand_command("/nope do x") == "/nope do x"


def _file_command(name: str, body: str) -> dict:
    return {
        "name": name,
        "description": "",
        "instruction_hint": "",
        "body": body,
        "skill_target": None,
    }


def test_expand_known_command_without_arg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _commands, "discover_commands", lambda: [_file_command("recap", "Recap it.")]
    )
    assert _commands.expand_command("/recap") == "Command /recap:\nRecap it."


def test_expand_known_command_with_arg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _commands, "discover_commands", lambda: [_file_command("recap", "Recap it.")]
    )
    assert _commands.expand_command("/recap just the PRs") == (
        "Command /recap:\nRecap it.\nAdditional message: just the PRs"
    )


# --- skill-as-command (source 0) ---


def _fake_skill(name: str, namespace: tuple[str, ...] = ()):
    return {"name": name, "description": f"{name} skill", "path": "/x", "namespace": namespace}


def test_skill_as_command_bare(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_commands, "_command_dirs", list)  # no file commands
    monkeypatch.setattr(_commands.skills, "_names", lambda: [_fake_skill("goal")])  # pyright: ignore[reportUnknownArgumentType]
    out = {c["name"]: c for c in _commands.discover_commands()}
    assert out["goal"]["skill_target"] == "goal"
    assert "ava.help(ava.skills.goal)" in _commands.expand_command("/goal")


def test_skill_as_command_namespaced(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_commands, "_command_dirs", list)
    monkeypatch.setattr(
        _commands.skills, "_names", lambda: [_fake_skill("brainstorming", ("demo",))]
    )
    out = {c["name"]: c for c in _commands.discover_commands()}
    assert "demo:brainstorming" in out
    assert out["demo:brainstorming"]["skill_target"] == "demo.brainstorming"
    expanded = _commands.expand_command("/demo:brainstorming a login form")
    assert "ava.help(ava.skills.demo.brainstorming)" in expanded
    assert expanded.endswith("Additional message: a login form")


def test_skill_as_command_dotted_input_still_resolves(monkeypatch: pytest.MonkeyPatch):
    """Backcompat: the pre-colon dotted spelling of a command name still
    expands — matching folds dash/underscore/colon."""
    monkeypatch.setattr(_commands, "_command_dirs", list)
    monkeypatch.setattr(
        _commands.skills, "_names", lambda: [_fake_skill("brainstorming", ("demo",))]
    )
    expanded = _commands.expand_command("/demo.brainstorming a login form")
    assert "ava.help(ava.skills.demo.brainstorming)" in expanded


def test_skill_as_command_hyphen_maps_to_underscore_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_commands, "_command_dirs", list)
    monkeypatch.setattr(
        _commands.skills,
        "_names",
        lambda: [_fake_skill("test-driven-development", ("demo",))],
    )
    out = _commands.expand_command("/demo.test-driven-development")
    assert "ava.help(ava.skills.demo.test_driven_development)" in out


def test_explicit_command_overrides_skill_as_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # a skill 'foo' AND an explicit foo.md — the file (source 1-5) wins over source 0
    _write(tmp_path, "foo.md", "explicit prompt body")
    monkeypatch.setattr(_commands, "_command_dirs", lambda: [(tmp_path, ())])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_commands.skills, "_names", lambda: [_fake_skill("foo")])  # pyright: ignore[reportUnknownArgumentType]
    out = {c["name"]: c for c in _commands.discover_commands()}
    assert out["foo"]["skill_target"] is None
    assert out["foo"]["body"] == "explicit prompt body"


# --- commands_enabled gate ---


def test_discover_empty_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path, "recap.md", "Recap it.")
    monkeypatch.setattr(_commands, "_command_dirs", lambda: [(tmp_path, ())])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_commands.skills, "_names", lambda: [_fake_skill("goal")])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_commands.settings.agent, "commands_enabled", False)
    assert _commands.discover_commands() == []


def test_expand_passthrough_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _commands, "discover_commands", lambda: [_file_command("recap", "Recap it.")]
    )
    monkeypatch.setattr(_commands.settings.agent, "commands_enabled", False)
    assert _commands.expand_command("/recap just the PRs") == "/recap just the PRs"


# --- multi-command chains (one message, several commands) ---


def _chain_catalog(monkeypatch: pytest.MonkeyPatch, *commands: dict) -> None:
    monkeypatch.setattr(_commands, "discover_commands", lambda: list(commands))  # pyright: ignore[reportUnknownArgumentType]


def test_chain_expands_every_command_in_the_order_typed(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    out = _commands.expand_command("/recap /plan")
    assert out.index("Command /recap:\nRecap it.") < out.index("Command /plan:\nPlan it.")
    # Reversing the input reverses the expansion — order is the typed order.
    flipped = _commands.expand_command("/plan /recap")
    assert flipped.index("Command /plan:") < flipped.index("Command /recap:")


def test_chain_gives_each_command_its_own_argument(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    out = _commands.expand_command("/recap just the PRs /plan the migration")
    assert "Command /recap:\nRecap it.\nAdditional message: just the PRs" in out
    assert "Command /plan:\nPlan it.\nAdditional message: the migration" in out


def test_chain_is_exactly_the_single_expansions_concatenated(monkeypatch: pytest.MonkeyPatch):
    # The mechanism adds nothing of its own around a chain — no preamble, no
    # framing, no instruction about how to treat the combination. A chain is
    # what each command expands to alone, joined by a blank line.
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    alone = [_commands.expand_command("/recap"), _commands.expand_command("/plan the migration")]
    assert _commands.expand_command("/recap /plan the migration") == "\n\n".join(alone)


def test_single_command_expands_unchanged(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(monkeypatch, _file_command("recap", "Recap it."))
    assert _commands.expand_command("/recap") == "Command /recap:\nRecap it."


def test_chain_mixes_file_command_and_skill_as_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_commands, "_command_dirs", list)
    monkeypatch.setattr(_commands.skills, "_names", lambda: [_fake_skill("goal")])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _commands,
        "discover_commands",
        lambda: [_file_command("recap", "Recap it."), *_commands._skill_commands().values()],
    )
    out = _commands.expand_command("/recap the week /goal")
    assert "Command /recap:\nRecap it.\nAdditional message: the week" in out
    assert "ava.help(ava.skills.goal)" in out


def test_slash_token_that_is_not_a_command_stays_in_the_argument(monkeypatch: pytest.MonkeyPatch):
    # A path in the free text is not a command boundary — only a registered
    # name opens the next segment.
    _chain_catalog(monkeypatch, _file_command("recap", "Recap it."))
    assert _commands.expand_command("/recap check /path/to/file") == (
        "Command /recap:\nRecap it.\nAdditional message: check /path/to/file"
    )


def test_missing_space_between_commands_is_one_unknown_name(monkeypatch: pytest.MonkeyPatch):
    # `/recap/plan` is a typo, not a chain: the whole token is the name, it
    # matches nothing, and the message passes through untouched.
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    assert _commands.expand_command("/recap/plan") == "/recap/plan"


def test_extra_whitespace_between_commands_still_chains(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    for raw in ("/recap    /plan", "/recap\n/plan"):
        out = _commands.expand_command(raw)
        assert "Command /recap:\nRecap it." in out
        assert "Command /plan:\nPlan it." in out
        assert "Additional message" not in out


def test_text_not_starting_with_a_command_never_chains(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(monkeypatch, _file_command("recap", "Recap it."))
    # A command name mid-sentence is prose, not an invocation.
    assert _commands.expand_command("check /recap output") == "check /recap output"


def test_unknown_leading_name_passes_the_whole_chain_through(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(monkeypatch, _file_command("recap", "Recap it."))
    assert _commands.expand_command("/nope /recap") == "/nope /recap"


def test_chain_expansion_is_source_neutral(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    assert "User" not in _commands.expand_command("/recap /plan")


def test_chain_passthrough_when_disabled(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(
        monkeypatch, _file_command("recap", "Recap it."), _file_command("plan", "Plan it.")
    )
    monkeypatch.setattr(_commands.settings.agent, "commands_enabled", False)
    assert _commands.expand_command("/recap /plan") == "/recap /plan"


# --- every command expands alike (no special cases) ---


def test_lifecycle_command_chains_like_any_other(monkeypatch: pytest.MonkeyPatch):
    """`/compact` tells the agent to replace its own context, which is the most
    disruptive thing a shipped command body says — and the expansion still
    treats it as an ordinary prompt template. No classification, no refusal, no
    warning: whether the instruction after it still applies is the agent's
    reading of the prompts, not a question the mechanism answers."""
    _chain_catalog(
        monkeypatch, _file_command("compact", "Wind down."), _file_command("recap", "Recap it.")
    )
    for raw in ("/compact /recap", "/recap /compact"):
        out = _commands.expand_command(raw)
        assert "Command /compact:\nWind down." in out
        assert "Command /recap:\nRecap it." in out
    # Order is still just the typed order — nothing gets hoisted or dropped.
    assert _commands.expand_command("/recap /compact").index("Command /recap:") < (
        _commands.expand_command("/recap /compact").index("Command /compact:")
    )


def test_repeating_one_command_expands_it_twice(monkeypatch: pytest.MonkeyPatch):
    _chain_catalog(monkeypatch, _file_command("compact", "Wind down."))
    assert _commands.expand_command("/compact /compact") == (
        "Command /compact:\nWind down.\n\nCommand /compact:\nWind down."
    )


def test_command_record_carries_no_chaining_policy():
    """The Command record has nowhere to put a per-command chaining rule, so the
    uniformity is structural rather than a convention the shipped files happen
    to follow. `skill_target` does branch the expansion, but it says what the
    command *is* (a skill rather than a template) and applies the same way to
    every skill-as-command — it is not a policy about which commands may share a
    message. A new field here means asking whether it is reintroducing one."""
    fields = set(_commands.Command.__annotations__)
    assert fields == {"name", "description", "instruction_hint", "body", "skill_target"}


# --- source-neutral expansion (agent-to-agent commands) ---


def test_expand_is_source_neutral(monkeypatch: pytest.MonkeyPatch):
    # Expansion must not name an actor — the envelope (wrap_inbound) attributes
    # the sender, so a /command sent by a peer agent reads correctly.
    monkeypatch.setattr(
        _commands, "discover_commands", lambda: [_file_command("recap", "Recap it.")]
    )
    monkeypatch.setattr(_commands.skills, "_names", lambda: [_fake_skill("goal")])  # pyright: ignore[reportUnknownArgumentType]
    for raw in ("/recap context", "/goal context"):
        assert "User" not in _commands.expand_command(raw)


# --- ava.agents.commands() discovery surface ---


def test_agents_commands_lists_name_desc_hint_no_body(monkeypatch: pytest.MonkeyPatch):
    from ava import agents

    monkeypatch.setattr(
        _commands,
        "discover_commands",
        lambda: [
            {
                "name": "recap",
                "description": "recap it",
                "instruction_hint": "a focus",
                "body": "SECRET BODY",
                "skill_target": None,
            }
        ],
    )
    out = agents.commands()
    assert len(out) == 1
    info = out[0]
    assert (info.name, info.description, info.instruction_hint) == ("recap", "recap it", "a focus")
    assert not hasattr(info, "body")
    assert str(info) == "/recap a focus  — recap it"
