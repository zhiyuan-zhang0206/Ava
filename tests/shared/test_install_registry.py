"""shared.install_registry unit tests — file-based ~/.ava/installed.json read/write + query.

Use `unit_home` fixture to point settings.general.ava_home to tmp, isolating the real registry file.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from shared import install_registry as reg
from shared.paths import install_registry_path
from shared.platform import LockTimeoutError


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

_REPO_ROOT = str(Path(__file__).resolve().parents[2])

# How long the slow writer sits between reading the registry and renaming its
# rewrite over it — the window a second writer's row has to be lost in.
_HOLD_S = 2.0


def _mutator_script(home: Path, name: str, *, marker: Path, wait_for_marker: bool) -> str:
    """A child that adds one package through a full `mutate` cycle.

    `save` stages the new registry into a temp sibling and renames it over the
    real path, so the read that built it and the rename that publishes it are
    two separate moments. A writer landing between them is erased: the rename
    publishes a registry read before that writer's row existed. The slow writer
    widens exactly that gap; the fast one aims for it.
    """
    stall = (
        f"_real_replace = os.replace\n"
        f"def _slow_replace(src, dst, **kw):\n"
        f"    if str(dst).endswith('installed.json'):\n"
        f"        pathlib.Path({str(marker)!r}).touch()\n"
        f"        time.sleep({_HOLD_S})\n"
        f"    return _real_replace(src, dst, **kw)\n"
        f"os.replace = _slow_replace\n"
    )
    wait = (
        f"marker = pathlib.Path({str(marker)!r})\n"
        f"deadline = time.monotonic() + 60\n"
        f"while not marker.exists():\n"
        f"    if time.monotonic() > deadline:\n"
        f"        raise SystemExit('the slow writer never reached its window')\n"
        f"    time.sleep(0.01)\n"
    )
    return (
        f"import os, sys, time, pathlib\n"
        f"sys.path.insert(0, {_REPO_ROOT!r})\n"
        f"os.environ['AVA_HOME'] = {str(home)!r}\n"
        f"{stall if not wait_for_marker else ''}"
        f"from shared import install_registry as reg\n"
        f"reg.load()\n"  # pay the import + first-read cost before the handshake
        f"{wait if wait_for_marker else ''}"
        f"with reg.mutate() as registry:\n"
        f"    registry.packages.append(\n"
        f"        reg.InstalledPackage(name={name!r}, type='skill', source='https://x')\n"
        f"    )\n"
    )


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
            _mutator_script(tmp_path, "alpha", marker=marker, wait_for_marker=False),
        ]
    )
    fast = subprocess.Popen(  # noqa: S603 — this interpreter, a literal script
        [
            sys.executable,
            "-c",
            _mutator_script(tmp_path, "beta", marker=marker, wait_for_marker=True),
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
