"""`ava plugins` + `ava skill` — package install/toggle lifecycle.

Builders plus their `_h_*` handlers for plugin config updates, external skill
package installs, and the supply-chain review pair (`scan` / `trust`).
Handlers lazy-import their `cmd_*` implementation from ``cli.commands`` so
parser building never loads Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_plugins_update(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_update

    return cmd_plugins_update()


def _h_plugins_install(args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_install

    return cmd_plugins_install(args.url, args.ref, args.path, accept_risk=args.accept_risk)


def _h_plugins_uninstall(args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_uninstall

    return cmd_plugins_uninstall(args.name)


def _h_plugins_installed(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_installed

    return cmd_plugins_installed()


def _h_plugins_inspect(args: argparse.Namespace) -> int:
    # Imported from its own module rather than the `cli.commands` package: the
    # catalog reaches into the agent layer, and routing it through the package
    # export would pull `agent` into every other CLI verb's import.
    from cli.commands.plugins_inspect import cmd_plugins_inspect

    return cmd_plugins_inspect(args.name)


def _h_plugins_upgrade(args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_upgrade

    return cmd_plugins_upgrade(args.name, force=args.force)


def _h_skill_update(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_update

    return cmd_skill_update(args.names, force=args.force)


def _h_skill_upgrade(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_upgrade

    return cmd_skill_upgrade(args.name, force=args.force)


def _h_plugins_enable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_enable

    return cmd_plugins_enable(args.name)


def _h_plugins_disable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_plugins_disable

    return cmd_plugins_disable(args.name)


def _h_skill_install(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_install

    return cmd_skill_install(args.source, args.ref, args.path, accept_risk=args.accept_risk)


def _h_skill_enable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_enable

    return cmd_skill_enable(args.name)


def _h_skill_disable(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_disable

    return cmd_skill_disable(args.name)


def _h_skill_register(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_register

    return cmd_skill_register(args.name, accept_risk=args.accept_risk)


def _h_skill_scan(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_scan

    return cmd_skill_scan(args.target)


def _h_skill_trust(args: argparse.Namespace) -> int:
    from cli.commands import cmd_skill_trust

    return cmd_skill_trust(args.name, revoke=args.revoke)


def _add_plugins_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_plugins_disable,
        _h_plugins_enable,
        _h_plugins_inspect,
        _h_plugins_install,
        _h_plugins_installed,
        _h_plugins_uninstall,
        _h_plugins_update,
        _h_plugins_upgrade,
    )

    # `ava plugins` — plugin config update + external skill package install lifecycle.
    plugins_p = sub.add_parser(
        "plugins",
        help="install + manage Ava plugins (accepts Ava-native skills and Claude Code plugin packages as input formats)",
    )
    plugins_sub = plugins_p.add_subparsers(dest="plugins_cmd", required=True)

    plugins_update_p = plugins_sub.add_parser(
        "update", help="auto-merge plugin config disk image schema diff"
    )
    plugins_update_p.set_defaults(func=_h_plugins_update)

    # `ava plugins install <url> [--path SUBDIR]` — clone an external package
    # (bare skill -> ~/.ava/skills/; Claude Code plugin -> ~/.ava/plugins/) and
    # record it in the install registry.
    plugins_install_p = plugins_sub.add_parser(
        "install",
        help="install an Ava plugin from a git source (input: Ava-native skill or Claude Code plugin package)",
    )
    plugins_install_p.add_argument("url", help="git URL of the source repo")
    plugins_install_p.add_argument(
        "--path", default=None, help="subdirectory of the source repo holding the package"
    )
    plugins_install_p.add_argument(
        "--ref", default=None, help="tag / commit / branch to pin (default: source default branch)"
    )
    plugins_install_p.add_argument(
        "--accept-risk",
        action="store_true",
        help="install despite critical security-scan findings (records which rules were waived)",
    )
    plugins_install_p.set_defaults(func=_h_plugins_install)

    plugins_uninstall_p = plugins_sub.add_parser(
        "uninstall", help="remove an installed plugin + its registry entry"
    )
    plugins_uninstall_p.add_argument("name", help="installed package name")
    plugins_uninstall_p.set_defaults(func=_h_plugins_uninstall)

    plugins_installed_p = plugins_sub.add_parser(
        "installed", aliases=["ls"], help="list install-registry entries"
    )
    plugins_installed_p.set_defaults(func=_h_plugins_installed)

    # `ava plugins inspect [name]` — the read-only catalog: what a plugin CAN
    # extend (surfaces + live signatures) and what each installed plugin DID
    # (registration facts, read off the attribution ledger). Loads this
    # machine's enabled plugins to read them, so it is a query, not a lifecycle
    # verb.
    plugins_inspect_p = plugins_sub.add_parser(
        "inspect",
        help="show the framework's extension surfaces + what each installed plugin registered",
    )
    plugins_inspect_p.add_argument(
        "name",
        nargs="?",
        default=None,
        help="plugin name — omit for the surface reference + one line per plugin",
    )
    plugins_inspect_p.set_defaults(func=_h_plugins_inspect)

    plugins_upgrade_p = plugins_sub.add_parser(
        "upgrade", help="re-fetch an installed plugin from its recorded source"
    )
    plugins_upgrade_p.add_argument("name", help="installed package name")
    plugins_upgrade_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite a locally modified copy instead of refusing",
    )
    plugins_upgrade_p.set_defaults(func=_h_plugins_upgrade)

    plugins_enable_p = plugins_sub.add_parser("enable", help="enable a plugin (local config)")
    plugins_enable_p.add_argument("name", help="plugin name")
    plugins_enable_p.set_defaults(func=_h_plugins_enable)

    plugins_disable_p = plugins_sub.add_parser("disable", help="disable a plugin (local config)")
    plugins_disable_p.add_argument("name", help="plugin name")
    plugins_disable_p.set_defaults(func=_h_plugins_disable)


def _add_skill_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_skill_disable,
        _h_skill_enable,
        _h_skill_install,
        _h_skill_register,
        _h_skill_scan,
        _h_skill_trust,
        _h_skill_update,
        _h_skill_upgrade,
    )

    # `ava skill` — install into + toggle $AVA_HOME/skills/ (the single skill
    # load dir). installed (dir on disk) and enabled (scanner loads it) are
    # orthogonal; removal lives under `ava plugins uninstall`.
    skill_p = sub.add_parser(
        "skill",
        help="install / enable / disable / register skills in the load dir ($AVA_HOME/skills/)",
    )
    skill_sub = skill_p.add_subparsers(dest="skill_cmd", required=True)

    # `ava skill install <src>` — the Agent Skills standard entry point: any
    # skill folder / skills repo (`skills/`, `.claude/skills/`) installs as
    # published, from a git URL or a local directory.
    skill_install_p = skill_sub.add_parser(
        "install",
        help="install Agent Skills standard skill(s) from a git URL or local directory",
    )
    skill_install_p.add_argument("source", help="git URL or local directory holding the skill(s)")
    skill_install_p.add_argument(
        "--path", default=None, help="subdirectory of the source holding the skill(s)"
    )
    skill_install_p.add_argument(
        "--ref", default=None, help="tag / commit / branch to pin (git sources; default branch)"
    )
    skill_install_p.add_argument(
        "--accept-risk",
        action="store_true",
        help="install despite critical security-scan findings (records which rules were waived)",
    )
    skill_install_p.set_defaults(func=_h_skill_install)

    # `ava skill update [name ...]` — the explicit update for repo-native
    # skills (R5): converge only lands missing copies; updating an existing
    # copy is this command. Conflict on local edits; `--force` overwrites.
    skill_update_p = skill_sub.add_parser(
        "update",
        help="update repo-native skills from this checkout (conflict on local edits; --force overwrites)",
    )
    skill_update_p.add_argument(
        "names", nargs="*", help="skill names to update (default: all repo-native skills)"
    )
    skill_update_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite locally modified copies with the repo version",
    )
    skill_update_p.set_defaults(func=_h_skill_update)

    # `ava skill upgrade <name>` — re-fetch a user-installed skill from its
    # recorded git source (a private skills repo etc.). Conflict on local
    # edits; `--force` overwrites.
    skill_upgrade_p = skill_sub.add_parser(
        "upgrade",
        help="re-fetch an installed skill from its recorded git source (conflict on local edits; --force overwrites)",
    )
    skill_upgrade_p.add_argument("name", help="installed skill package name")
    skill_upgrade_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite a locally modified copy instead of refusing",
    )
    skill_upgrade_p.set_defaults(func=_h_skill_upgrade)

    skill_enable_p = skill_sub.add_parser(
        "enable", help="surface a tracked package to the skill scanner"
    )
    skill_enable_p.add_argument("name", help="tracked package name")
    skill_enable_p.set_defaults(func=_h_skill_enable)

    skill_disable_p = skill_sub.add_parser(
        "disable", help="hide a tracked package from the skill scanner (stays on disk)"
    )
    skill_disable_p.add_argument("name", help="tracked package name")
    skill_disable_p.set_defaults(func=_h_skill_disable)

    skill_register_p = skill_sub.add_parser(
        "register",
        help="track a dir already under $AVA_HOME/skills/ so the scanner loads it (origin=user)",
    )
    skill_register_p.add_argument("name", help="directory name under $AVA_HOME/skills/")
    skill_register_p.add_argument(
        "--accept-risk",
        action="store_true",
        help="register despite critical security-scan findings (records which rules were waived)",
    )
    skill_register_p.set_defaults(func=_h_skill_register)

    # `ava skill scan` / `ava skill trust` — the supply-chain review pair: read
    # what the scanner saw, then vouch for the content as a human.
    skill_scan_p = skill_sub.add_parser(
        "scan",
        help="re-run the supply-chain scan over an installed package or any directory",
    )
    skill_scan_p.add_argument("target", help="tracked package name, or a path to a skill directory")
    skill_scan_p.set_defaults(func=_h_skill_scan)

    skill_trust_p = skill_sub.add_parser(
        "trust", help="record that a human read this package's content (trust=reviewed)"
    )
    skill_trust_p.add_argument("name", help="tracked package name")
    skill_trust_p.add_argument(
        "--revoke", action="store_true", help="take the review back (trust=unreviewed)"
    )
    skill_trust_p.set_defaults(func=_h_skill_trust)
