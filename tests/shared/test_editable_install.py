"""Regression guards for the editable-install pointer shared by Ava lifecycle paths."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from shared import editable_install


def _write_pth(source_root: Path, target: Path) -> Path:
    pth = source_root / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(target))
    return pth


def test_poisoned_missing_target_is_repaired_and_emits_warning_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dangling target must not survive a guard pass without an audit event."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    pth.chmod(0o444)
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    editable_install.repair_editable_ava_pth(source_root)

    assert pth.read_text() == str(source_root)
    assert stat.S_IMODE(pth.stat().st_mode) == 0o444
    assert emitted == [
        (
            ("telemetry", "editable_pth_repaired"),
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "pth_path": str(pth),
                    "poisoned_target": str(tmp_path / "deleted-worktree"),
                    "source_root": str(source_root),
                },
            },
        )
    ]


def test_worktree_below_allowlisted_dev_clone_is_still_repaired(tmp_path: Path) -> None:
    """Allowlisting a clone root must not implicitly allow its disposable worktrees."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    worktree = dev_clone / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    pth = _write_pth(source_root, worktree)

    editable_install.repair_editable_ava_pth(source_root, allowed_roots=(dev_clone,))

    assert pth.read_text() == str(source_root)


def test_exact_allowlisted_dev_clone_target_is_left_unchanged(tmp_path: Path) -> None:
    """The allowlist is exact-root based, so an approved stable clone remains legal."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    pth = _write_pth(source_root, dev_clone)

    editable_install.repair_editable_ava_pth(source_root, allowed_roots=(dev_clone,))

    assert pth.read_text() == str(dev_clone)


def test_repair_leaves_a_legal_single_line_pointer_byte_identical(tmp_path: Path) -> None:
    """A single allowed source line is legal regardless of a trailing newline."""

    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    pth = _write_pth(source_root, dev_clone)
    pth.write_text(str(dev_clone))

    editable_install.repair_editable_ava_pth(source_root, allowed_roots=(dev_clone,))

    assert pth.read_bytes() == str(dev_clone).encode()


def test_repair_leaves_uv_native_repeated_pth_entries_byte_identical(tmp_path: Path) -> None:
    """uv's one-line-per-wheel pointer is legal when every line is allowed."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text(f"{source_root}\n{source_root}")
    before = pth.read_bytes()

    assert editable_install.editable_install_violations(source_root) == ()
    assert editable_install.repair_editable_ava_pth(source_root) == ()

    assert pth.read_bytes() == before


def test_repair_leaves_a_legal_source_pointer_without_trailing_newline(tmp_path: Path) -> None:
    """Pointer legality is semantic rather than a trailing-newline byte form."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    pth.write_text(str(source_root))

    editable_install.repair_editable_ava_pth(source_root)

    assert pth.read_bytes() == str(source_root).encode()


def test_crlf_semantic_pth_is_read_without_repair(tmp_path: Path) -> None:
    """Universal-newline reading accepts a Windows pointer without changing its bytes."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_bytes(f"{source_root}\r\n".encode())
    before = pth.read_bytes()

    assert editable_install.editable_install_violations(source_root) == ()
    assert editable_install.repair_editable_ava_pth(source_root) == ()
    assert pth.read_bytes() == before


def test_empty_pth_is_reported_and_repaired_to_source_content(tmp_path: Path) -> None:
    """An empty pointer cannot be interpreted as a legal editable target."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text("")

    violations = editable_install.editable_install_violations(source_root)
    editable_install.repair_editable_ava_pth(source_root)

    assert len(violations) == 1
    assert "names" in violations[0]
    assert pth.read_bytes() == str(source_root).encode()


def _write_direct_url(source_root: Path, url: str, *, editable: bool = True) -> Path:
    """A direct_url.json matching uv's editable-install record shape."""
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text(json.dumps({"url": url, "dir_info": {"editable": editable}}))
    return du


def test_missing_direct_url_beside_pth_is_reported_and_repaired(tmp_path: Path) -> None:
    """A removed direct-URL record is the reverse half-uninstall direction."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    direct_url.unlink()

    violations = editable_install.editable_install_violations(source_root)
    repairs = editable_install.repair_editable_install(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata missing" in violations[0]
    assert json.loads(direct_url.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }
    assert repairs == (
        editable_install.EditableInstallRepair(
            path=direct_url,
            poisoned_target="(missing)",
            source_root=source_root,
        ),
    )


def test_missing_dist_info_beside_pth_is_reported_without_fabrication(tmp_path: Path) -> None:
    """A missing directory supplies no version, so only uv may recreate it."""

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)

    violations = editable_install.editable_install_violations(source_root)
    repairs = editable_install.repair_editable_install(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata missing" in violations[0]
    assert repairs == ()
    assert not list(pth.parent.glob("ava-*.dist-info"))


def test_editable_console_script_violations_follow_the_posix_venv_layout(tmp_path: Path) -> None:
    """The POSIX launcher is judged beside the POSIX venv interpreter."""

    source_root = tmp_path / "source"
    interpreter = source_root / ".venv" / "bin" / "python"
    console_script = interpreter.with_name("ava")
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    console_script.touch()

    assert editable_install.editable_console_script_violations(source_root) == ()

    console_script.unlink()

    assert editable_install.editable_console_script_violations(source_root) == (
        f"{console_script} editable console script missing",
    )


def test_editable_console_script_violations_follow_the_windows_venv_layout(tmp_path: Path) -> None:
    """A Windows tree is inspected structurally even on a POSIX test host."""

    source_root = tmp_path / "source"
    interpreter = source_root / ".venv" / "Scripts" / "python.exe"
    console_script = interpreter.with_name("ava.exe")
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()

    assert editable_install.editable_console_script_violations(source_root) == (
        f"{console_script} editable console script missing",
    )

    console_script.touch()

    assert editable_install.editable_console_script_violations(source_root) == ()


def test_poisoned_direct_url_is_repaired_and_emits_warning_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A direct_url naming a worktree is the same poison recorded elsewhere."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())
    du.chmod(0o444)
    original = du.read_text()
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }
    assert stat.S_IMODE(du.stat().st_mode) == 0o444
    assert emitted == [
        (
            ("telemetry", "editable_direct_url_repaired"),
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "direct_url_path": str(du),
                    "poisoned_target": original.strip(),
                    "source_root": str(source_root),
                },
            },
        )
    ]


def test_healthy_direct_url_is_left_unchanged(tmp_path: Path) -> None:
    """A direct_url naming the source root itself must survive a guard pass."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, source_root.as_uri())
    original = du.read_text()

    assert editable_install.repair_editable_direct_url(source_root) == ()
    assert du.read_text() == original


def test_exact_allowlisted_dev_clone_direct_url_is_left_unchanged(
    tmp_path: Path,
) -> None:
    """The exact-root allowlist applies to the direct_url record too."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    du = _write_direct_url(source_root, dev_clone.as_uri())
    original = du.read_text()

    editable_install.repair_editable_direct_url(source_root, allowed_roots=(dev_clone,))

    assert du.read_text() == original


def test_worktree_below_allowlisted_dev_clone_direct_url_is_repaired(
    tmp_path: Path,
) -> None:
    """Allowlisting a clone root must not implicitly allow its disposable worktrees."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    worktree = dev_clone / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    du = _write_direct_url(source_root, worktree.as_uri())

    editable_install.repair_editable_direct_url(source_root, allowed_roots=(dev_clone,))

    assert json.loads(du.read_text())["url"] == source_root.as_uri()


def test_unparsable_direct_url_is_repaired(tmp_path: Path) -> None:
    """A truncated or garbage record must not survive without a repair."""
    source_root = tmp_path / "prod" / "source"
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text("{not json")

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }


def test_non_editable_direct_url_is_repaired(tmp_path: Path) -> None:
    """A record denying editability contradicts the pointer beside it."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, source_root.as_uri(), editable=False)

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }


def test_repair_editable_install_repairs_pointer_and_direct_url(tmp_path: Path) -> None:
    """The combined entry point leaves neither record pointing at a worktree."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    du = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())

    repairs = editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert json.loads(du.read_text())["url"] == source_root.as_uri()
    assert len(repairs) == 2


def test_repair_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """The atomic-write repair must not accumulate temp files in site-packages."""
    source_root = tmp_path / "prod" / "source"
    _write_pth(source_root, tmp_path / "deleted-worktree")
    _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())

    editable_install.repair_editable_install(source_root)

    assert list(source_root.rglob("*.tmp")) == []


# ─── read-only violations report (public twin of the repair primitives) ─────


def test_editable_install_violations_healthy_is_empty(tmp_path: Path) -> None:
    """Legal records (prod root + editable URL) report no violations."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_accepts_repeated_checkout_pth_entries(
    tmp_path: Path,
) -> None:
    """Repeated allowed entries are uv's normal one-line-per-wheel layout."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text(f"{source_root}\n{source_root}")

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_both_missing_records_are_empty(tmp_path: Path) -> None:
    """A venv with no editable records has nothing to assert or repair."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)

    assert editable_install.editable_install_violations(source_root) == ()
    assert editable_install.repair_editable_install(source_root) == ()


def test_half_uninstall_pointer_missing_beside_metadata_is_repaired(tmp_path: Path) -> None:
    """Existing editable metadata makes a missing pointer a repairable violation."""
    source_root = tmp_path / "prod" / "source"
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    pth = direct_url.parent.parent / editable_install.EDITABLE_PTH_NAME

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata present but pointer missing" in violations[0]
    repairs = editable_install.repair_editable_install(source_root)
    assert pth.read_text() == str(source_root)
    assert repairs == (
        editable_install.EditableInstallRepair(
            path=pth,
            poisoned_target="(missing)",
            source_root=source_root,
        ),
    )


def test_editable_install_violations_allowlisted_clone_is_empty(tmp_path: Path) -> None:
    """Exact-root allowlisting applies to the read-only report too."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    dev_clone = tmp_path / "Ava"
    _write_pth(source_root, dev_clone)
    _write_direct_url(source_root, dev_clone.as_uri())

    assert (
        editable_install.editable_install_violations(source_root, allowed_roots=(dev_clone,)) == ()
    )


def test_editable_install_violations_reports_poisoned_records(tmp_path: Path) -> None:
    """Both records are reported, each naming the disposable source."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, worktree)
    record = _write_direct_url(source_root, worktree.as_uri())

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 2
    assert str(pth) in violations[0] and str(worktree) in violations[0]
    assert str(record) in violations[1] and str(worktree) in violations[1]


def test_editable_install_violations_unparsable_record_is_reported(
    tmp_path: Path,
) -> None:
    """A record that cannot be parsed cannot be verified — reported."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    record = _write_direct_url(source_root, source_root.as_uri())
    record.write_text("{not json")

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1 and str(record) in violations[0]


def test_editable_import_gate_requires_the_checkout_editable_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate rejects a poisoned pointer and a cwd decoy outside the checkout."""

    source_root = tmp_path / "source"
    agent_package = source_root / "agent"
    agent_package.mkdir(parents=True)
    (agent_package / "__init__.py").write_text("")
    (agent_package / "exec_child.py").write_text("VALUE = 'source'\n")
    subprocess.run(  # noqa: S603 — test-owned interpreter and venv path
        [sys.executable, "-m", "venv", str(source_root / ".venv")],
        check=True,
        capture_output=True,
    )
    interpreter = editable_install._venv_python(source_root)
    assert interpreter is not None
    site_packages = Path(
        subprocess.check_output(  # noqa: S603 — test-owned interpreter and venv path
            [str(interpreter), "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    pth = site_packages / editable_install.EDITABLE_PTH_NAME
    pth.write_text(f"{source_root}\n")

    assert editable_install.editable_import_gate(source_root) == ()

    allowed_root = tmp_path / "Ava"
    allowed_agent = allowed_root / "agent"
    allowed_agent.mkdir(parents=True)
    (allowed_agent / "__init__.py").write_text("")
    (allowed_agent / "exec_child.py").write_text("VALUE = 'allowed'\n")
    pth.write_text(str(allowed_root))

    assert (
        editable_install.editable_import_gate(
            source_root,
            allowed_roots=(allowed_root,),
        )
        == ()
    )

    pth.write_text(f"{tmp_path / 'missing'}\n")
    assert editable_install.editable_import_gate(source_root)

    neutral_dir = tmp_path / "neutral"
    decoy = neutral_dir / "agent"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("")
    (decoy / "exec_child.py").write_text("VALUE = 'decoy'\n")
    pth.write_text(f"{neutral_dir}\n")
    monkeypatch.setattr(editable_install.tempfile, "gettempdir", lambda: str(neutral_dir))
    violations = editable_install.editable_import_gate(source_root)

    assert len(violations) == 1
    assert "path=" in violations[0]
    assert str(decoy / "exec_child.py") in violations[0]

    for candidate in (
        source_root / ".venv" / "bin" / "python3",
        source_root / ".venv" / "bin" / "python",
        source_root / ".venv" / "Scripts" / "python.exe",
    ):
        candidate.unlink(missing_ok=True)

    assert editable_install.editable_import_gate(source_root) == ("venv python missing",)


@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required for the native editable test"
)
def test_uv_native_editable_records_are_legal_and_exec_guard_accepts_them(tmp_path: Path) -> None:
    """A real uv install keeps its per-wheel pointer byte-identical through both guards."""

    checkout = Path(__file__).parents[2]
    uv_built_venv_root = tmp_path / "uv-built-venv"
    venv.create(uv_built_venv_root / ".venv", with_pip=False, symlinks=True)
    interpreter = uv_built_venv_root / ".venv" / "bin" / "python"
    install = subprocess.run(  # noqa: S603 — test-owned venv and checkout path
        [
            "env",
            "-u",
            "VIRTUAL_ENV",
            "uv",
            "pip",
            "install",
            "-e",
            str(checkout),
            "--python",
            str(interpreter),
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    pth_paths = editable_install.editable_ava_pth_paths(uv_built_venv_root)
    assert pth_paths
    before = {path: path.read_bytes() for path in pth_paths}
    assert (
        editable_install.editable_install_violations(
            uv_built_venv_root,
            allowed_roots=(checkout,),
        )
        == ()
    )
    assert (
        editable_install.guard_editable_install(
            uv_built_venv_root,
            allowed_roots=(checkout,),
        )
        == ()
    )
    assert {path: path.read_bytes() for path in pth_paths} == before

    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(checkout)
    child = subprocess.run(  # noqa: S603 — current test interpreter and test-owned code
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from shared.editable_install import guard_editable_install\n"
            f"raise SystemExit(bool(guard_editable_install(Path({str(uv_built_venv_root)!r}), "
            f"allowed_roots=(Path({str(checkout)!r}),))))\n",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert child.returncode == 0, child.stderr


def test_current_interpreter_source_root_reads_a_posix_venv_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exec guard anchors itself to the interpreter's checkout, not cwd."""
    source_root = tmp_path / "checkout"
    interpreter = source_root / ".venv" / "bin" / "python"
    monkeypatch.setattr(editable_install.sys, "executable", str(interpreter))

    assert editable_install.current_interpreter_source_root() == source_root


def test_current_interpreter_source_root_keeps_symlinked_venv_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolving the base interpreter must not discard its enclosing virtualenv."""
    source_root = tmp_path / "checkout"
    base_python = tmp_path / "base" / "python"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("")
    interpreter = source_root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(base_python)
    monkeypatch.setattr(editable_install.sys, "executable", str(interpreter))

    assert editable_install.current_interpreter_source_root() == source_root


def test_guard_editable_install_repairs_all_records_and_emits_exec_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One exec-boundary guard reports then repairs the complete poisoned install."""
    source_root = tmp_path / "prod" / "source"
    deleted_worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, deleted_worktree)
    direct_url = _write_direct_url(source_root, deleted_worktree.as_uri())
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    violations = editable_install.guard_editable_install(source_root)

    assert len(violations) == 2
    assert pth.read_text() == str(source_root)
    assert json.loads(direct_url.read_text())["url"] == source_root.as_uri()
    assert [entry[0][1] for entry in emitted] == [
        "editable_pth_repaired",
        "editable_direct_url_repaired",
        "exec_editable_install_poisoned",
    ]
    assert emitted[-1] == (
        ("telemetry", "exec_editable_install_poisoned"),
        {
            "level": "warning",
            "source": "exec_guard",
            "attributes": {
                "violations": list(violations),
                "source_root": str(source_root),
                "python": str(editable_install.sys.executable),
            },
        },
    )


def test_guard_editable_install_recovers_a_half_uninstall(tmp_path: Path) -> None:
    """The exec boundary repairs a pointer uv removed before it can import Ava."""
    source_root = tmp_path / "prod" / "source"
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    pth = direct_url.parent.parent / editable_install.EDITABLE_PTH_NAME

    violations = editable_install.guard_editable_install(source_root)

    assert len(violations) == 1
    assert "half-uninstalled" in violations[0]
    assert pth.read_text() == str(source_root)


def test_guard_editable_install_repairs_with_registered_real_emitter(tmp_path: Path) -> None:
    """The exec guard must repair through the real telemetry contract wiring."""
    source_root = tmp_path / "prod" / "source"
    deleted_worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, deleted_worktree)
    direct_url = _write_direct_url(source_root, deleted_worktree.as_uri())

    violations = editable_install.guard_editable_install(source_root)

    assert len(violations) == 2
    assert pth.read_text() == str(source_root)
    assert json.loads(direct_url.read_text())["url"] == source_root.as_uri()


def test_guard_editable_install_repairs_when_telemetry_emit_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Telemetry drift must not block the repair that restores exec imports."""
    source_root = tmp_path / "prod" / "source"
    deleted_worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, deleted_worktree)
    direct_url = _write_direct_url(source_root, deleted_worktree.as_uri())

    def raise_exec_guard_emit(*args: object, **_kwargs: object) -> None:
        if args[1] == "exec_editable_install_poisoned":
            raise ValueError("unregistered telemetry event")

    monkeypatch.setattr("shared.telemetry.emit", raise_exec_guard_emit)

    violations = editable_install.guard_editable_install(source_root)

    assert len(violations) == 2
    assert pth.read_text() == str(source_root)
    assert json.loads(direct_url.read_text())["url"] == source_root.as_uri()


def test_guard_editable_install_leaves_healthy_records_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean per-exec check has no repair side effect or telemetry noise."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    pth = _write_pth(source_root, source_root)
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    before = {pth: pth.read_bytes(), direct_url: direct_url.read_bytes()}
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    assert editable_install.guard_editable_install(source_root) == ()
    assert {path: path.read_bytes() for path in before} == before
    assert emitted == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not Windows ACLs")
def test_editable_pth_write_window_allows_atomic_replacement_and_restores_modes(
    tmp_path: Path,
) -> None:
    """Only the sanctioned window opens a protected uv replacement boundary."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    site_packages = pth.parent
    site_packages.chmod(0o555)
    pth.chmod(0o444)

    with pytest.raises(PermissionError):
        pth.unlink()

    with editable_install.editable_pth_write_window(source_root):
        assert stat.S_IMODE(site_packages.stat().st_mode) == 0o755
        assert stat.S_IMODE(pth.stat().st_mode) == 0o644
        pth.unlink()
        replacement = pth.with_name(f".{pth.name}.tmp")
        replacement.write_text("replacement")
        replacement.replace(pth)

    assert pth.read_text() == "replacement"
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
    assert stat.S_IMODE(pth.stat().st_mode) == 0o444


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not Windows ACLs")
def test_editable_pth_write_window_opens_hardened_dist_info_directory(
    tmp_path: Path,
) -> None:
    """A hardened 0o555 ava dist-info dir must be writable inside the window.

    Regression for the 2026-09-03 rollout: a converged host carried a read-only
    ``ava-*.dist-info`` directory, and uv's reinstall uninstall removes the
    files *inside* that directory (INSTALLER, RECORD, ...) before it can
    rewrite the distribution — a write operation that needs owner write on the
    dist-info directory itself, not only on its parent site-packages. The
    window must open it and restore the exact original mode afterwards.
    """

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    dist_info = direct_url.parent
    site_packages = pth.parent
    installer = dist_info / "INSTALLER"
    installer.write_text("uv\n")
    site_packages.chmod(0o555)
    dist_info.chmod(0o555)

    with pytest.raises(PermissionError):
        installer.unlink()

    with editable_install.editable_pth_write_window(source_root):
        assert stat.S_IMODE(site_packages.stat().st_mode) == 0o755
        assert stat.S_IMODE(dist_info.stat().st_mode) == 0o755
        installer.unlink()
        installer.write_text("uv\n")

    assert installer.read_text() == "uv\n"
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
    assert stat.S_IMODE(dist_info.stat().st_mode) == 0o555


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not Windows ACLs")
def test_editable_site_packages_dirs_finds_protected_directory_without_ava_records(
    tmp_path: Path,
) -> None:
    """A partial sync cannot hide its protected directory by deleting records."""
    source_root = tmp_path / "prod" / "source"
    site_packages = source_root / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    site_packages.chmod(0o555)

    assert editable_install.editable_site_packages_dirs(source_root) == (site_packages,)


def test_write_window_skips_path_that_disappears_before_entry(tmp_path: Path) -> None:
    """A venv recreation race cannot abort the sync before it starts."""
    vanished_path = tmp_path / "vanished.pth"
    vanished_path.write_text("temporary")
    vanished_path.unlink()

    with editable_install._write_window((vanished_path,)):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not Windows ACLs")
def test_repair_editable_install_opens_protected_site_packages_directory(
    tmp_path: Path,
) -> None:
    """Converge repair remains able to fix records after directory hardening."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    direct_url = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())
    site_packages = pth.parent
    site_packages.chmod(0o555)

    editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert json.loads(direct_url.read_text())["url"] == source_root.as_uri()
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not Windows ACLs")
def test_repair_half_uninstall_opens_protected_site_packages_directory(
    tmp_path: Path,
) -> None:
    """The write window can recreate a missing pointer after directory hardening."""
    source_root = tmp_path / "prod" / "source"
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    pth = direct_url.parent.parent / editable_install.EDITABLE_PTH_NAME
    site_packages = pth.parent
    site_packages.chmod(0o555)

    editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
