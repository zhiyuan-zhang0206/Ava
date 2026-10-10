"""base.packages.extensions.install_registry unit tests — file-based ~/.ava/installed.json read/write + query.

Use `unit_home` fixture to point AVA_HOME to tmp, isolating the real registry file.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from base.native_process.os_platform import LockTimeoutError
from base.packages.extensions import install_registry as reg
from base.paths import install_registry_path


def _pkg(
    name: str, *, type: reg.PackageType = "skill", enabled: bool = True, ref: str | None = None
):
    return reg.InstalledPackage(
        name=name, type=type, source=f"https://x/{name}", ref=ref, enabled=enabled
    )


def test_load_missing_file_is_empty(unit_home: Path) -> None:
    assert not install_registry_path().exists()
    assert reg.load().packages == []


def test_register_save_load_roundtrip(unit_home: Path) -> None:
    reg.register(_pkg("alpha", ref="v1"))
    reg.register(_pkg("beta", enabled=False))
    loaded = reg.load()
    assert {p.name for p in loaded.packages} == {"alpha", "beta"}
    assert install_registry_path().exists()


def test_register_replaces_same_name(unit_home: Path) -> None:
    reg.register(_pkg("dup", ref="v1"))
    reg.register(_pkg("dup", ref="v2"))
    pkgs = reg.load().packages
    assert len(pkgs) == 1
    assert pkgs[0].ref == "v2"


def test_get_returns_entry_or_none(unit_home: Path) -> None:
    reg.register(_pkg("here"))
    found = reg.get("here")
    assert found is not None and found.name == "here"
    assert reg.get("absent") is None


def test_deregister_reports_presence(unit_home: Path) -> None:
    reg.register(_pkg("gone"))
    assert reg.deregister("gone") is True
    assert reg.deregister("gone") is False
    assert reg.get("gone") is None


def test_enabled_skill_names_filters_type_and_enabled(unit_home: Path) -> None:
    """Skill AND plugin entries gate the load dir (a plugin's converged skills
    namespace rides the plugin's own entry); mcp entries and disabled ones don't."""
    reg.register(_pkg("on-skill", type="skill", enabled=True))
    reg.register(_pkg("off-skill", type="skill", enabled=False))
    reg.register(_pkg("on-plugin", type="plugin", enabled=True))
    reg.register(_pkg("off-plugin", type="plugin", enabled=False))
    reg.register(_pkg("on-mcp", type="mcp", enabled=True))
    assert reg.enabled_skill_names() == {"on-skill", "on-plugin"}


def test_malformed_json_raises(unit_home: Path) -> None:
    install_registry_path().write_text("{ not json", encoding="utf-8")
    with pytest.raises(reg.SchemaInvalid):
        reg.load()


def test_empty_file_is_empty_registry(unit_home: Path) -> None:
    install_registry_path().write_text("   \n", encoding="utf-8")
    assert reg.load().packages == []


def test_save_is_atomic_and_leaves_no_temp(unit_home: Path) -> None:
    """save() stages through a temp sibling + rename (audit #2): no `.tmp`
    lingers, and a stale temp from a crashed earlier writer is swept."""
    stale = install_registry_path().with_name("installed.json.tmp")
    stale.write_text("{ truncated", encoding="utf-8")

    reg.register(_pkg("alpha"))
    reg.register(_pkg("beta", enabled=False))

    assert not stale.exists()
    loaded = reg.load()
    assert {p.name for p in loaded.packages} == {"alpha", "beta"}
    # the on-disk file is complete, parseable JSON
    import json

    json.loads(install_registry_path().read_text(encoding="utf-8"))


def test_load_raises_on_rows_that_fold_to_one_key(unit_home: Path) -> None:
    """Dash and underscore are one name (design R2-B1): `ava-code` and
    `ava_code` as separate rows is the dual-row state that used to crash the
    skill scanner fleet-wide (audit 02 #4) — the read refuses it now."""
    import json

    install_registry_path().write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "ava-code", "type": "skill"},
                    {"name": "ava_code", "type": "skill"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(reg.DuplicatePackageName):
        reg.load()


def test_load_accepts_spelling_variants_that_do_not_collide(unit_home: Path) -> None:
    """Distinct keys stay distinct — only true folding duplicates refuse."""
    reg.register(_pkg("ava-code"))
    reg.register(_pkg("ava-fleet"))
    assert {p.name for p in reg.load().packages} == {"ava-code", "ava-fleet"}


# ─── the cross-process registry lock ───

_REPO_ROOT = str(Path(__file__).resolve().parents[4])

# How long the slow writer sits between reading the registry and renaming its
# rewrite over it — the window a second writer's row has to be lost in.
_HOLD_S = 2.0


# A child that adds one package through a full `mutate` cycle.
#
# `save` stages the new registry into a temp sibling and renames it over the
# real path, so the read that built it and the rename that publishes it are
# two separate moments. A writer landing between them is erased: the rename
# publishes a registry read before that writer's row existed. The slow writer
# widens exactly that gap; the fast one aims for it.
#
# Literal source with data in argv (see `_mutator_argv`), so test selection can
# read the probe's imports.
_MUTATOR_SCRIPT = """
import os, sys, time, pathlib
root, home, name, marker_path, hold_s, role = sys.argv[1:]
sys.path.insert(0, root)
os.environ['AVA_HOME'] = home
if role == 'slow':
    _real_replace = os.replace
    def _slow_replace(src, dst, **kw):
        if str(dst).endswith('installed.json'):
            pathlib.Path(marker_path).touch()
            time.sleep(float(hold_s))
        return _real_replace(src, dst, **kw)
    os.replace = _slow_replace
elif role != 'fast':
    raise SystemExit(f'unknown mutator role: {role!r}')
from base.packages.extensions import install_registry as reg
reg.load()  # pay the import + first-read cost before the handshake
if role == 'fast':
    marker = pathlib.Path(marker_path)
    deadline = time.monotonic() + 60
    while not marker.exists():
        if time.monotonic() > deadline:
            raise SystemExit('the slow writer never reached its window')
        time.sleep(0.01)
with reg.mutate() as registry:
    registry.packages.append(
        reg.InstalledPackage(name=name, type='skill', source='https://x')
    )
"""


def _mutator_argv(home: Path, name: str, *, marker: Path, wait_for_marker: bool) -> list[str]:
    """The trailing argv for `_MUTATOR_SCRIPT`: the fast writer waits, the slow one stalls."""
    role = "fast" if wait_for_marker else "slow"
    return [_REPO_ROOT, str(home), name, str(marker), str(_HOLD_S), role]


def test_concurrent_mutators_in_separate_processes_both_survive(tmp_path: Path) -> None:
    """Two OS processes adding different packages keep both rows.

    `ava skill install` from an agent's shell, `ava converge` on a restart, and
    the gateway's skills-toggle handler are three processes mutating one
    `installed.json`. `save` being atomic only guarantees no torn file — it does
    nothing about a lost update, and a row lost here is a package that stops
    being tracked while its directory is still on disk, which is exactly the
    state the skill scanner refuses to load.
    """
    marker = tmp_path / "slow-mutator-in-window"
    slow = subprocess.Popen(  # noqa: S603 — this interpreter, a literal script
        [
            sys.executable,
            "-c",
            _MUTATOR_SCRIPT,
            *_mutator_argv(tmp_path, "alpha", marker=marker, wait_for_marker=False),
        ]
    )
    fast = subprocess.Popen(  # noqa: S603 — this interpreter, a literal script
        [
            sys.executable,
            "-c",
            _MUTATOR_SCRIPT,
            *_mutator_argv(tmp_path, "beta", marker=marker, wait_for_marker=True),
        ]
    )
    try:
        assert fast.wait(timeout=120) == 0
        assert slow.wait(timeout=120) == 0
    finally:
        for proc in (slow, fast):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    written = (tmp_path / "installed.json").read_text(encoding="utf-8")
    assert "alpha" in written, "the stalled writer's own row did not land"
    assert "beta" in written, (
        "the second process's row was overwritten by the first process's stale "
        "rewrite — the registry read-modify-write is not serialized across processes"
    )


def test_mutate_is_not_reentrant_within_one_process(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested cycle raises instead of deadlocking — the constraint every
    `mutate` body must respect (no `register` / `deregister` inside one).

    The bound is patched down to 0.3s to keep this quick. In production it is
    30s, so the real cost of a nested cycle is a 30-second stall before the
    error — bounded, but not fail-fast.
    """
    monkeypatch.setattr(reg, "_REGISTRY_LOCK_TIMEOUT_S", 0.3)
    with reg.mutate(), pytest.raises(LockTimeoutError), reg.mutate():
        pass


# ── schema v2: migration + update-policy resolution ─────────────────────


def test_v1_file_is_refused(unit_home: Path) -> None:
    """The lazy v1 migration is retired: a v1 file fails fast, never loads."""
    import json

    path = install_registry_path()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "packages": [
                    {
                        "name": "alpha",
                        "type": "skill",
                        "origin": "repo",
                        "origin_path": "/x/ava_builtins/skills/alpha",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(reg.SchemaInvalid, match="different shape"):
        reg.load()


def test_newer_schema_version_is_refused(unit_home: Path) -> None:
    import json

    install_registry_path().write_text(json.dumps({"version": 3, "packages": []}), encoding="utf-8")
    with pytest.raises(reg.SchemaInvalid):
        reg.load()


def test_resolved_policy_resolves_from_source_class(unit_home: Path) -> None:
    from base.config import settings

    repo_pkg = reg.InstalledPackage(
        name="r", type="skill", origin="repo", origin_path="/x/ava_builtins/skills/r"
    )
    pol = reg.resolved_policy(repo_pkg)
    assert pol.channel == "core"
    assert pol.mode == settings.packages.refresh_default_mode
    assert pol.interval_seconds == settings.packages.refresh_default_interval_seconds

    git_pkg = reg.InstalledPackage(name="g", type="skill", origin="user", source="https://x/g")
    assert reg.resolved_policy(git_pkg).channel == "git"

    local_pkg = reg.InstalledPackage(name="l", type="skill", origin="user")
    local_pol = reg.resolved_policy(local_pkg)
    assert local_pol.channel is None
    assert local_pol.mode == "off"
    assert local_pol.interval_seconds is None


def test_resolved_policy_plugin_channel_follows_origin_path(unit_home: Path) -> None:
    from base import paths

    inside = reg.InstalledPackage(
        name="p",
        type="skill",
        origin="plugin",
        origin_path=str(paths.repo_root() / "ava_builtins" / "plugins" / "ava_code" / "skills"),
    )
    assert reg.resolved_policy(inside).channel == "core"

    outside = reg.InstalledPackage(
        name="q", type="skill", origin="plugin", origin_path="/elsewhere/plugins/q/skills"
    )
    outside_pol = reg.resolved_policy(outside)
    assert outside_pol.channel is None
    assert outside_pol.mode == "off"


def test_resolved_policy_explicit_values_win(unit_home: Path) -> None:
    pkg = reg.InstalledPackage(name="r", type="skill", origin="repo")
    pkg.update.mode = "notify"
    pkg.update.interval_seconds = 3600
    pkg.update.channel = "git"  # a deliberately odd pin: explicit wins over provenance
    pol = reg.resolved_policy(pkg)
    assert (pol.channel, pol.mode, pol.interval_seconds) == ("git", "notify", 3600)


def test_differing_paths_names_changed_added_removed_and_skips_noise(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "keep").mkdir(parents=True)
        (root / "keep" / "x.md").write_text("same")
        (root / "__pycache__").mkdir()
    (a / "changed.md").write_text("old")
    (b / "changed.md").write_text("new")
    (a / "only_a.md").write_text("a")
    (b / "only_b.md").write_text("b")
    (a / "__pycache__" / "junk.pyc").write_text("a")
    (b / "__pycache__" / "junk.pyc").write_text("b")
    assert reg.differing_paths(a, b) == ["changed.md", "only_a.md", "only_b.md"]
    assert reg.differing_paths(a, b, skip_subtrees=frozenset({("changed.md",)})) == [
        "only_a.md",
        "only_b.md",
    ]
