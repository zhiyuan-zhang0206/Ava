"""`ava plugins inspect` — render the extension-surface catalog.

The reader is an agent as often as an operator (a plugin-authoring agent needs
the injection surfaces and their signatures without reading framework source), so
the output is line-oriented and greppable rather than pretty: every line starts
with a stable token (`surface`, `plugin`, `entry`, `note`, a diff status), one
fact per line, no boxes and no columns that shift with content width.

The computation lives in `agent.plugin_catalog`; this module only formats it.
"""

from __future__ import annotations

import sys
import textwrap

from agent.plugin_catalog import (
    DECLARATION_ONLY_KEYS,
    SURFACES,
    Catalog,
    PluginView,
    Surface,
    UnknownPlugin,
    build_catalog,
    declared_vs_registered,
    entry_point_signature,
)

_WRAP = 96
_LABEL = 13
_INDENT = " " * (2 + _LABEL)


def cmd_plugins_inspect(name: str | None) -> int:
    """`ava plugins inspect [name]` — the extension surfaces plus what each
    installed plugin registered on this machine.

    No name: the surface reference and one line per plugin. With a name: that
    plugin's full contribution list and, when it ships an `ava-plugin.json`, its
    declared-vs-registered diff. A query, not a lifecycle verb: nothing is
    installed or removed, and drift is reported rather than enforced (that gate
    is plugin-spec-v2 S3). Reading registrations does load the plugins, with the
    side effects `build_catalog` names.
    """
    catalog = build_catalog()
    if name is None:
        _print_surfaces(catalog)
        _print_plugin_directory(catalog)
        return 0
    try:
        view = catalog.plugin(name)
    except UnknownPlugin as e:
        print(f"[ava plugins inspect] {e}", file=sys.stderr)
        return 1
    _print_plugin_detail(view)
    return 0


def _cell(text: str, width: int) -> str:
    """Pad to `width`, and when the content is wider, still separate it from the
    next column — an over-long state channel name must not fuse with its type."""
    return f"{text:<{width}}" if len(text) < width else f"{text}  "


def _wrapped(label: str, text: str) -> str:
    """`  label   text`, wrapped to `_WRAP` with the continuation lines aligned
    under the text rather than the label."""
    return textwrap.fill(
        text, width=_WRAP, initial_indent=f"  {_cell(label, _LABEL)}", subsequent_indent=_INDENT
    )


def _print_surfaces(catalog: Catalog) -> None:
    """The framework's extension points, with live signatures."""
    print("## extension surfaces")
    print("# every signature below is rendered from the live register function, not transcribed")
    for surface in SURFACES:
        print()
        print(f"surface {surface.id}")
        for entry_point in surface.entry_points:
            # Signatures stay on one line even when they run long: an agent greps
            # `entry ` for the exact call, and a wrapped signature is not greppable.
            print(f"  {_cell('entry', _LABEL)}{entry_point_signature(entry_point)}")
        if surface.protocol is not None:
            print(_wrapped("protocol", surface.protocol))
        print(_wrapped("note", surface.note))
        print(_wrapped("manifest", _manifest_key_line(surface)))
        print(_wrapped("registered", _surface_usage(catalog, surface)))


def _manifest_key_line(surface: Surface) -> str:
    if surface.manifest_key is None:
        return "no ava-plugin.json contributions key declares this surface"
    return f"contributions.{surface.manifest_key}"


def _surface_usage(catalog: Catalog, surface: Surface) -> str:
    """`3 by ava_code, ava_memory` — who is using this surface right now."""
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
    """One line per installed plugin, plus its description underneath."""
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
    print("# `ava plugins inspect <name>` for one plugin's full contribution list")


def _summary(view: PluginView) -> str:
    if not view.enabled:
        return "(not loaded)"
    counts = view.surface_counts()
    if not counts:
        return "(registers nothing)"
    return ", ".join(f"{surface} {n}" for surface, n in counts.items())


def _print_plugin_detail(view: PluginView) -> None:
    """One plugin: identity, every registered fact, then the manifest diff."""
    print(f"plugin {view.name}")
    print(f"  {_cell('enabled', _LABEL)}{str(view.enabled).lower()}")
    print(
        f"  {_cell('source', _LABEL)}{'builtin' if view.builtin else 'external'}  {view.directory}"
    )
    if view.description:
        print(_wrapped("description", view.description))
    if view.manifest is None:
        print(_wrapped("manifest", "none — no ava-plugin.json, so nothing to diff against"))
    else:
        m = view.manifest
        print(
            f"  {_cell('manifest', _LABEL)}{m.name} {m.version}  "
            f"engines.ava={m.engines.get('ava', '?')}"
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
    # This catalog is built by importing the plugin, so it can only report what
    # was REGISTERED. How often each of these rows actually FIRED — philosophy
    # §6's obsolescence gauge — lives in the event stream, keyed by the same
    # surface/identifier; point the reader at it rather than dialing Loki from
    # a catalog command.
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
    """The declared-vs-registered comparison, plus the two categories that are
    deliberately NOT drift."""
    print()
    print("declared vs registered")
    if view.manifest is None:
        print("  (no manifest — a plugin without ava-plugin.json declares nothing)")
        return
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
                f"{', '.join(declared_only)} — declared, and settled on disk at install "
                "time rather than by a register_* call (not comparable here)",
            )
        )
