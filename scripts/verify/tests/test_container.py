"""The Linux verification container keeps to its boundary, offline.

The boundary rules (future/infra/verification-boundaries.md): nothing of the host's
cluster, credentials or container runtime enters; nothing is published; no secret
reaches an image layer or the container; the image runs as an ordinary user and
carries no source. A real run needs Docker and several minutes; these tests fail
the moment a change to the recipe would break one of those rules.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from base.host.env.config_lite_table import FIELD_ALIASES
from scripts.verify import boundary, container

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILE = _REPO_ROOT / "scripts/verify/Dockerfile"


def _run_argv(objects: Path) -> list[str]:
    return container.docker_run_argv("ava-verify-test", "ava-verify:test", objects, "8g", "1g")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed git argv in a temporary repository
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ------------------------------------------------------------------ host state refused


@pytest.mark.parametrize(
    "relative",
    [".", "..", ".ava", ".ava/source", ".ssh", ".gnupg", ".aws", ".docker"],
)
def test_host_home_and_its_sealed_directories_are_refused(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".ava/source").mkdir(parents=True)
    for name in (".ssh", ".gnupg", ".aws", ".docker"):
        (home / name).mkdir()
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="refusing to mount host state"):
        boundary.refuse_host_state(home / relative)
    with pytest.raises(ValueError, match="refusing to mount host state"):
        _run_argv(home / relative)


def test_the_container_runtime_socket_is_refused(tmp_path: Path) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    with pytest.raises(ValueError, match="refusing to mount host state"):
        boundary.refuse_host_state(socket)


def test_a_repository_object_store_inside_home_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    objects = home / "Ava/.git/objects"
    objects.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    boundary.refuse_host_state(objects)


# ----------------------------------------------------------------- the one docker run


def test_the_run_publishes_nothing_and_carries_no_environment_or_credential(
    tmp_path: Path,
) -> None:
    objects = tmp_path / "repo/.git/objects"
    objects.mkdir(parents=True)
    argv = _run_argv(objects)
    joined = " ".join(argv)

    assert argv[:4] == ["docker", "run", "-d", "--init"]
    for forbidden in ("-p", "--publish", "-P", "--publish-all", "-e", "--env", "--env-file"):
        assert forbidden not in argv
    assert "--network" not in argv
    assert "--privileged" not in argv
    assert "docker.sock" not in joined
    assert ".ssh" not in joined
    assert ".ava" not in joined
    assert argv[argv.index("--cap-drop") + 1] == "ALL"


def test_the_only_bind_mount_is_the_object_store_read_only(tmp_path: Path) -> None:
    objects = tmp_path / "repo/.git/objects"
    objects.mkdir(parents=True)
    argv = _run_argv(objects)

    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
    assert mounts == [f"type=bind,src={objects},dst={container.OBJECTS_MOUNT},ro"]
    volumes = [argv[i + 1] for i, arg in enumerate(argv) if arg in {"-v", "--volume"}]
    assert volumes == [f"{container.CACHE_VOLUME}:{container.CONTAINER_HOME}/.cache"]


def test_the_object_store_of_a_linked_worktree_is_the_common_one(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _git(tmp_path, "init", "--initial-branch=main", str(main))
    _git(main, "commit", "--allow-empty", "-m", "seed")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "--detach", str(linked))

    assert container.git_objects_dir(linked) == (main / ".git/objects").resolve()


def test_a_ref_cannot_smuggle_a_git_option(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--initial-branch=main", str(repo))
    _git(repo, "commit", "--allow-empty", "-m", "seed")

    assert boundary.resolve_commit(repo, "HEAD") == _git(repo, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        boundary.resolve_commit(repo, "--all")


# ------------------------------------------------------------------ the image definition


def _dockerfile_lines() -> list[str]:
    lines = _DOCKERFILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_the_image_runs_as_an_ordinary_user() -> None:
    lines = _dockerfile_lines()
    users = [line for line in lines if line.startswith("USER ")]
    assert users, "the image must switch away from root"
    assert users[-1] != "USER root"
    assert not users[-1].endswith(("0", "root"))
    assert any(line.startswith("RUN useradd ") for line in lines)
    assert lines.index(users[-1]) > max(i for i, line in enumerate(lines) if "useradd" in line)
    assert lines[-1].startswith("CMD ")


def test_the_image_user_is_not_named_like_the_schema_owner_role() -> None:
    # initdb makes the OS user the bootstrap superuser; `ava start` refuses it when its
    # name is the schema owner role `ava`.
    lines = _dockerfile_lines()
    (create,) = (line for line in lines if line.startswith("RUN useradd "))
    user = create.split()[-1]
    assert user != "ava"
    assert f"USER {user}" in lines
    assert f"/home/{user}" == container.CONTAINER_HOME


def test_the_image_copies_only_its_provisioning_inputs() -> None:
    copies = [
        line.split()[1:-1] for line in _dockerfile_lines() if line.startswith(("COPY ", "ADD "))
    ]
    sources = {src for sources in copies for src in sources}
    assert sources == set(container.CONTEXT_TREES)
    ignore = (_REPO_ROOT / "scripts/verify/Dockerfile.dockerignore").read_text(encoding="utf-8")
    allowed = {line[1:] for line in ignore.splitlines() if line.startswith("!")}
    assert allowed == set(container.CONTEXT_TREES)


def test_the_image_bakes_in_no_key_or_token() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(ARG|ENV)\s+\S*(KEY|TOKEN|SECRET|PASSWORD)", text, re.M | re.I)
    assert "--mount=type=secret" not in text


def test_the_image_tag_follows_what_the_build_can_see(tmp_path: Path) -> None:
    for name in (
        *container.CONTEXT_FILES,
        "scripts/provision/node.sh",
        "scripts/install-cli-tools.sh",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (tmp_path / "scripts/other.py").write_text("unrelated")
    before = container.context_digest(tmp_path)

    (tmp_path / "scripts/other.py").write_text("changed, but outside the build context")
    assert container.context_digest(tmp_path) == before
    (tmp_path / "scripts/provision/node.sh").write_text("changed")
    assert container.context_digest(tmp_path) != before


# ---------------------------------------------------------------------- the start profile


def test_the_profile_admits_no_provider_key_and_only_known_configuration() -> None:
    assert not [key for key in boundary.PROFILE if re.search(r"KEY|TOKEN|SECRET|PASSWORD", key)]
    # `ava init --config-file` refuses any key that is not a Settings field alias.
    assert set(boundary.PROFILE) <= set(FIELD_ALIASES.values())


def test_the_start_selects_the_services_the_observer_checks() -> None:
    from scripts.verify import observe

    assert set(boundary.SERVICES) == observe.SERVICES
    selected = [
        container.START_ARGV[i + 1]
        for i, arg in enumerate(container.START_ARGV)
        if arg == "--only-service"
    ]
    assert selected == list(boundary.SERVICES)
    assert "--worktree" not in container.START_ARGV


def test_init_records_the_identity_and_start_takes_only_the_selection() -> None:
    """The recipe runs `ava init` (machine, capabilities, profile) and then `ava start`
    with nothing but the service selection: the same split the CLI parsers enforce."""
    from cli.parsers import build_parser

    assert container.INIT_ARGV[:2] == [".venv/bin/ava", "init"]
    assert container.START_ARGV[:2] == [".venv/bin/ava", "start"]
    parser = build_parser()
    init = parser.parse_args(container.INIT_ARGV[1:])
    assert init.machine_name == boundary.MACHINE_NAME
    assert init.serve_gateway is True and init.serve_agent_runner is True
    assert init.config_file == f"{container.WORK}/profile.env"
    start = parser.parse_args(container.START_ARGV[1:])
    assert start.only_service == list(boundary.SERVICES)
    assert {"init", "start"} <= set(container.TIMEOUTS)  # every step a run records is bounded
