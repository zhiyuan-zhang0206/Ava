from __future__ import annotations

import sys
import textwrap
import traceback

from agent.extensions.catalog import (
    DECLARATION_ONLY_KEYS,
    SURFACES,
    Catalog,
    PluginView,
    Surface,
    UnknownPluginError,
    build_catalog,
    declared_vs_registered,
    entry_point_signature,
)

_WRAP = 96
_LABEL = 13
_INDENT = " " * (2 + _LABEL)


# meiva:ref cli/commands/extensions/docs/plugin-inspection.meiva.okf.md#inspection-output-and-verification
def cmd_plugins_inspect(name: str | None) -> int:
    catalog = build_catalog()
    if name is None:
        _print_surfaces(catalog)
        _print_plugin_directory(catalog)
        return 0
    try:
        view = catalog.plugin(name)
    except UnknownPluginError as e:
        print(f"[meiva plugins inspect] {e}", file=sys.stderr)
        return 1
    _print_plugin_detail(view)
    return 0


def _cell(text: str, width: int) -> str:
    return f"{text:<{width}}" if len(text) < width else f"{text}  "


def _wrapped(label: str, text: str) -> str:
    return textwrap.fill(
        text, width=_WRAP, initial_indent=f"  {_cell(label, _LABEL)}", subsequent_indent=_INDENT
    )


def _print_surfaces(catalog: Catalog) -> None:
    print("## extension surfaces")
    print("# every signature below is rendered from the live register function, not transcribed")
    for surface in SURFACES:
        print()
        print(f"surface {surface.id}")
        for entry_point in surface.entry_points:
            print(f"  {_cell('entry', _LABEL)}{entry_point_signature(entry_point)}")
        if surface.protocol is not None:
            print(_wrapped("protocol", surface.protocol))
        print(_wrapped("note", surface.note))
        print(_wrapped("manifest", _manifest_key_line(surface)))
        print(_wrapped("registered", _surface_usage(catalog, surface)))


def _manifest_key_line(surface: Surface) -> str:
    if surface.manifest_key is None:
        return "no meiva-plugin.json contributions key declares this surface"
    return f"contributions.{surface.manifest_key}"


def _surface_usage(catalog: Catalog, surface: Surface) -> str:
    users = [
        f"{view.name} {n}"
        for view in catalog.plugins
        if (n := view.surface_counts().get(surface.id))
    ]
    if not users:
        return "nothing registered here"
    total = sum(view.surface_counts().get(surface.id, 0) for view in catalog.plugins)
    return f"{total} — {', '.join(users)}"


def _print_plugin_directory(catalog: Catalog) -> None:
    enabled = sum(1 for v in catalog.plugins if v.enabled)
    print()
    print(f"## installed plugins ({len(catalog.plugins)}, {enabled} enabled)")
    print("# a disabled plugin is never imported, so it has no registrations to report")
    for view in catalog.plugins:
        state = "enabled" if view.enabled else "disabled"
        origin = "builtin" if view.builtin else "external"
        print()
        print(f"plugin {view.name}  {state}  {origin}  {_summary(view)}")
        if view.description:
            print(
                textwrap.fill(
                    view.description, width=_WRAP, initial_indent="  ", subsequent_indent="  "
                )
            )
    print()
    print("# `meiva plugins inspect <name>` for one plugin's full contribution list")


def _summary(view: PluginView) -> str:
    if not view.enabled:
        return "(not loaded)"
    counts = view.surface_counts()
    if not counts:
        return "(registers nothing)"
    return ", ".join(f"{surface} {n}" for surface, n in counts.items())


def _print_plugin_detail(view: PluginView) -> None:
    print(f"plugin {view.name}")
    print(f"  {_cell('enabled', _LABEL)}{str(view.enabled).lower()}")
    print(
        f"  {_cell('source', _LABEL)}{'builtin' if view.builtin else 'external'}  {view.directory}"
    )
    if view.description:
        print(_wrapped("description", view.description))
    m = view.manifest
    print(
        f"  {_cell('manifest', _LABEL)}{m.name} {m.version}  "
        f"engines.meiva={m.engines.get('meiva', '?')}"
    )

    print()
    if not view.enabled:
        print("registered contributions: none — this plugin is disabled and never imported")
        return
    print(f"registered contributions ({len(view.contributions)})")
    if not view.contributions:
        print("  (none — the plugin loaded but called no register_* entry point)")
    for c in view.contributions:
        print(f"  {_cell(c.surface, 21)}{_cell(c.identifier, 26)}{c.detail}")
    if view.contributions:
        print(
            _wrapped(
                "note",
                "counts of how often each row fired (per model) come from `plugin_activation` "
                "events — see the metrics report's plugin_activation section",
            )
        )

    _print_diff(view)


def _print_diff(view: PluginView) -> None:
    print()
    print("declared vs registered")
    for entry in declared_vs_registered(view):
        print(f"  {_cell(entry.status, 25)}{entry.surface}/{entry.identifier}")

    undeclarable = sorted(
        {
            c.surface
            for c in view.contributions
            if next(s for s in SURFACES if s.id == c.surface).manifest_key is None
        }
    )
    if undeclarable:
        print(
            _wrapped(
                "undeclarable",
                f"{', '.join(undeclarable)} — registered here, and the spec-v2 "
                "contributions grammar has no key for them (not drift)",
            )
        )
    declared_only = sorted(k for k in DECLARATION_ONLY_KEYS if k in view.manifest.contributions)
    if declared_only:
        print(
            _wrapped(
                "install-time",
                f"{', '.join(declared_only)} — declared, and read straight from the "
                "manifest (files settled on disk at install time, `ui` by the console) "
                "rather than by a register_* call (not comparable here)",
            )
        )


def _where(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return f" (at {frames[-1].filename}:{frames[-1].lineno})" if frames else ""


# meiva:ref cli/commands/extensions/docs/plugin-inspection.meiva.okf.md#inspection-output-and-verification
def cmd_plugins_verify() -> int:
    from agent.state import plugin_state_schema
    from meiva.sdk_surface.extensions import load_extensions

    failures: list[tuple[str, BaseException]] = []
    try:
        loaded = load_extensions(
            report=lambda name, exc: failures.append((name, exc)),
            validate_state=plugin_state_schema,
        )
    except Exception as exc:
        print("RESULT enabled=0 failed=0 rc=2")
        print(f"TOOL-ERROR {type(exc).__name__}: {str(exc)[:200]}")
        return 2
    enabled = sum(1 for entry in loaded.config.plugins.values() if entry.enabled)
    rc = 1 if failures else 0
    print(f"RESULT enabled={enabled} failed={len(failures)} rc={rc}")
    for name, exc in failures:
        print(f"RED plugin={name} {type(exc).__name__}: {exc}{_where(exc)}".replace("\n", " "))
    return rc
