"""Tests for cli/enroll.py — settings-independent agent-runner enrollment."""

import ast
import inspect
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli import enroll
from shared.env_registry import WSL_DEFAULT_HEALTH_PORT_BASE, health_port_env_aliases

_REAL_CHECK_BROWSER_DEPS = enroll._check_browser_deps
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _stub_browser_deps_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing enrollment tests hermetic on hosts without browser deps."""
    monkeypatch.setattr(enroll, "_check_browser_deps", lambda: None)


@pytest.fixture(autouse=True)
def _restore_cluster_secret_env() -> Iterator[None]:
    """`run_enroll` mirrors the resolved secret input into os.environ for the
    verification fetch (cli/enroll.py); undo it so the test process keeps the
    suite's pinned value (tests/conftest.py) — home_isolation guards exactly
    this class of leak."""
    saved = os.environ.get("AVA_CLUSTER_SECRET")
    yield
    if saved is None:
        os.environ.pop("AVA_CLUSTER_SECRET", None)
    else:
        os.environ["AVA_CLUSTER_SECRET"] = saved


def test_enroll_module_does_not_import_settings() -> None:
    # enroll must run on a fresh host where Settings cannot be built; statically
    # guard that it imports neither shared.config nor cli.commands.
    tree = ast.parse(inspect.getsource(enroll))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
    assert "shared.config" not in imported
    assert not any(m.startswith("cli.commands") for m in imported)


def test_shared_package_init_does_not_import_settings() -> None:
    # `from shared import bootstrap` works only if shared/__init__.py also
    # avoids transitively loading shared.config. Guard that it stays bare.
    import shared

    tree = ast.parse(inspect.getsource(shared))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
    assert "shared.config" not in imported, (
        "shared/__init__.py must stay bare so `from shared import bootstrap` works on a "
        "fresh host before Settings can be built; found shared.config in imports"
    )


def test_enroll_import_stays_settings_free_with_a_poisoned_browser_env(tmp_path: Path) -> None:
    """The fresh-host import must not construct Settings from an invalid .env.

    A transitive module-level shared.config import would make this subprocess
    fail before enrollment could repair its host dependencies; AST guards alone
    cannot observe that runtime import closure.
    """
    ava_home = tmp_path / "ava-home"
    fake_home = tmp_path / "home"
    ava_home.mkdir()
    fake_home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", "import cli.enroll; print('settings-free import ok')"],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "AVA_BROWSER_ENABLED": "definitely-not-a-bool",
            "AVA_HOME": str(ava_home),
            "HOME": str(fake_home),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "settings-free import ok"


def test_first_bootstrap_write_stays_settings_free_with_a_poisoned_browser_env(
    tmp_path: Path,
) -> None:
    """Fresh enrollment must write its first .env without constructing Settings.

    Import-only coverage misses the snapshot path that runs during
    ``write_bootstrap_env``. A module-level settings dependency there rejects a
    malformed browser setting before the fresh host can persist enrollment.
    """
    ava_home = tmp_path / "ava-home"
    fake_home = tmp_path / "home"
    ava_home.mkdir()
    fake_home.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from pathlib import Path; from cli.enroll import write_bootstrap_env; "
            "path = Path(os.environ['AVA_HOME']) / '.env'; "
            "write_bootstrap_env(path, gateway='https://cp', machine_name='runner'); "
            "assert path.exists(); print('settings-free bootstrap write ok')",
        ],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "AVA_BROWSER_ENABLED": "definitely-not-a-bool",
            "AVA_HOME": str(ava_home),
            "HOME": str(fake_home),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "settings-free bootstrap write ok"


def test_write_bootstrap_env_required_fields(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    enroll.write_bootstrap_env(
        p,
        gateway="https://cp",
        machine_name="wsl-gpu",
    )
    text = p.read_text()
    assert "AVA_GATEWAY_URL=https://cp" in text
    assert "AVA_MACHINE_NAME=wsl-gpu" in text
    assert "AVA_MACHINE_SERVE_AGENT_RUNNER=true" in text
    # AVA_CONFIG_SOURCE is gone (2026-08-01): the config source is role-derived,
    # so the bootstrap env needs no source marker.
    assert "AVA_CONFIG_SOURCE" not in text


def test_write_bootstrap_env_creates_owner_only_file(tmp_path: Path) -> None:
    """The bootstrap file contains the cluster bearer secret, so it must be
    private at creation time rather than tightened after cleartext is written."""
    path = tmp_path / ".env"
    enroll.write_bootstrap_env(
        path,
        gateway="https://cp",
        machine_name="runner",
        cluster_secret="sek",  # noqa: S106 — inert test credential
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_bootstrap_env_replacement_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement leaves the prior enrolled state intact and does
    not strand a cleartext temporary file beside it."""
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=1\n")

    def fail_replace(_source: Path, _target: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        enroll.write_bootstrap_env(
            path,
            gateway="https://cp",
            machine_name="runner",
            cluster_secret="sek",  # noqa: S106 — inert test credential
        )

    assert path.read_text() == "ORIGINAL=1\n"
    assert list(tmp_path.glob("..env.*")) == []


def test_write_bootstrap_env_preserves_machine_local_keys(tmp_path: Path) -> None:
    """A re-enroll rewrites the bootstrap block and drops cluster facts, and
    NOTHING else: model API keys, AVA_REQUIRE_* opt-outs, a prior health-port
    block, and operator comments all survive verbatim. The old whole-file
    replace silently wiped this unit's model credentials and machine-scope
    opt-outs on every re-enroll (fleet Windows box, 2026-08-19)."""
    p = tmp_path / ".env"
    p.write_text(
        "AVA_GATEWAY_URL=http://old-gateway:8000\n"
        "# model credentials stay local (windows-setup.md)\n"
        "ANTHROPIC_API_KEY=sk-local\n"
        "AVA_CROSS_MACHINE_TRANSFER_BACKEND=none\n"
        "AVA_DB_URL=postgresql://stale@old:5433/ava\n"
        "AVA_AGENT_HOST_HEALTH_PORT=18117\n"
    )
    enroll.write_bootstrap_env(
        p,
        gateway="https://cp",
        machine_name="n",
        cluster_secret="sek",  # noqa: S106 — inert test credential
        cluster_keys=frozenset({"AVA_DB_URL", "AVA_REDIS_URL"}),
    )
    text = p.read_text()
    assert "ANTHROPIC_API_KEY=sk-local" in text
    assert "AVA_CROSS_MACHINE_TRANSFER_BACKEND=none" in text
    assert "# model credentials stay local (windows-setup.md)" in text
    assert "AVA_AGENT_HOST_HEALTH_PORT=18117" in text
    assert "AVA_DB_URL" not in text  # a cluster fact the runner must not cache
    assert text.count("AVA_GATEWAY_URL=") == 1  # owned: rewritten, not duplicated
    assert "AVA_GATEWAY_URL=https://cp" in text


def test_write_bootstrap_env_rewrite_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("ANTHROPIC_API_KEY=sk-local\n")
    enroll.write_bootstrap_env(
        p,
        gateway="https://cp",
        machine_name="n",
        cluster_secret="sek",  # noqa: S106 — inert test credential
    )
    first = p.read_text()
    enroll.write_bootstrap_env(
        p,
        gateway="https://cp",
        machine_name="n",
        cluster_secret="sek",  # noqa: S106 — inert test credential
    )
    assert p.read_text() == first
    assert first.count("ANTHROPIC_API_KEY=") == 1


def test_bare_reenroll_keeps_a_prior_ssl_bundle(tmp_path: Path) -> None:
    """Like the health-port block: a prior explicit --ssl-cert-file survives a
    bare re-enroll, and only a restated one replaces it."""
    p = tmp_path / ".env"
    enroll.write_bootstrap_env(
        p, gateway="https://cp", machine_name="n", ssl_cert_file="/etc/ssl/cert.pem"
    )
    enroll.write_bootstrap_env(p, gateway="https://cp", machine_name="n")
    assert "SSL_CERT_FILE=/etc/ssl/cert.pem" in p.read_text()
    enroll.write_bootstrap_env(
        p, gateway="https://cp", machine_name="n", ssl_cert_file="/etc/ssl/other.pem"
    )
    text = p.read_text()
    assert text.count("SSL_CERT_FILE=") == 1
    assert "SSL_CERT_FILE=/etc/ssl/other.pem" in text


def test_write_bootstrap_env_omits_ssl_cert_when_absent(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    enroll.write_bootstrap_env(p, gateway="https://cp", machine_name="n")
    text = p.read_text()
    assert "SSL_CERT_FILE" not in text


def test_write_bootstrap_env_includes_ssl_cert_when_present(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    enroll.write_bootstrap_env(
        p,
        gateway="https://cp",
        machine_name="n",
        ssl_cert_file="/etc/ssl/cert.pem",
    )
    text = p.read_text()
    assert "SSL_CERT_FILE=/etc/ssl/cert.pem" in text
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/cert.pem" in text


def test_run_enroll_writes_env_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)

    def fake_fetch(gateway, timeout=10.0, role=None):
        # The enroll fetch requests the RUNNER projection (Task #1236) — the
        # runner's AVA_DB_URL must be the least-privilege ava_runner one.
        assert role == "runner"
        return {"AVA_DB_URL": "postgresql://x/y", "AVA_REDIS_URL": "redis://x/0"}

    monkeypatch.setattr(enroll.bootstrap, "fetch_bootstrap_config", fake_fetch)  # pyright: ignore[reportUnknownArgumentType]
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 0
    assert env_path.exists()
    # machine_host is persisted to the $AVA_HOME/machine_host file (not the .env)
    # so a re-enroll, which rewrites the .env, cannot wipe this host's address.
    assert (tmp_path / "machine_host").read_text().strip() == "10.0.0.9"
    assert "AVA_MACHINE_HOST" not in env_path.read_text()
    # the fetched cluster facts are NOT cached into .env (2026-08-01 refactor):
    # the runner re-fetches them at every process start
    assert "AVA_DB_URL" not in env_path.read_text()
    assert "AVA_REDIS_URL" not in env_path.read_text()


def test_run_enroll_reads_cluster_secret_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented path keeps the bearer secret out of argv and shell
    history by injecting it through AVA_CLUSTER_SECRET."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    # cli.enroll intentionally reads the raw pre-Settings bootstrap environment.
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "from-environment")
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType]
            "AVA_DB_URL": "postgresql://ava@10.0.0.5:5433/ava",
            "AVA_REDIS_URL": "redis://ava@10.0.0.5:6380/0",
        },
    )

    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://10.0.0.5:8000",
            "--machine-name",
            "runner",
            "--machine-host",
            "10.0.0.9",
        ]
    )

    assert rc == 0
    assert "AVA_CLUSTER_SECRET=from-environment" in env_path.read_text()


def test_run_enroll_reports_browser_deps_ready_after_writing_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A capable enrolled host gets a probe-scoped browser readiness confirmation."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(enroll, "_check_browser_deps", _REAL_CHECK_BROWSER_DEPS)
    monkeypatch.setattr(enroll, "ensure_browser_deps", lambda: None)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_args, **_kwargs: {"AVA_DB_URL": "postgresql://ava@db:5432/ava"},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "runner",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )

    assert rc == 0
    assert env_path.exists()
    assert (
        "browser deps OK (Chrome + npx on PATH) — ava-browser should start on this host"
        in capsys.readouterr().out
    )


def test_run_enroll_prints_browser_warning_to_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An incapable host enrolls successfully but cannot silently skip ava-browser."""
    env_path = tmp_path / ".env"
    reason = "no npx (install Node.js for chrome-devtools-mcp)"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(enroll, "_check_browser_deps", _REAL_CHECK_BROWSER_DEPS)
    monkeypatch.setattr(enroll, "ensure_browser_deps", lambda: reason)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_args, **_kwargs: {"AVA_DB_URL": "postgresql://ava@db:5432/ava"},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "runner",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert env_path.exists()
    assert reason in captured.out
    assert "ava-browser will not run on this host until this is fixed" in captured.out
    assert reason in captured.err
    assert "ava-browser will not run on this host until this is fixed" in captured.err


def test_run_enroll_prints_a_notice_without_a_repair_box_when_display_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Headless enrollment stays successful without telling operators to install Node."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(enroll, "_check_browser_deps", _REAL_CHECK_BROWSER_DEPS)
    monkeypatch.setattr(
        enroll, "ensure_browser_deps", lambda: "no display (WSL without WSLg / headless server)"
    )
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_args, **_kwargs: {"AVA_DB_URL": "postgresql://ava@db:5432/ava"},  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "runner",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert env_path.exists()
    assert "not applicable" in captured.out
    assert "Install Node.js" not in captured.out
    assert "Install Node.js" not in captured.err


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("false", True),
        ("0", True),
        ("no", True),
        ("off", True),
        ("f", True),
        ("n", True),
        ("true", False),
        ("1", False),
        ("yes", False),
        ("on", False),
    ],
)
def test_browser_explicitly_disabled_matches_pydantic_falsey_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """Enrollment honors every false-y spelling Settings treats as browser-disabled."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    if value is not None:
        env_path.write_text(f"AVA_BROWSER_ENABLED = {value} \n")
    assert enroll._browser_explicitly_disabled() is expected


def test_run_enroll_requires_machine_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a reachable address the runner would register its ops endpoint at
    # localhost and the gateway would dial itself — argparse fails loud instead.
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", tmp_path / ".env")
    with pytest.raises(SystemExit):
        enroll.run_enroll(["--gateway", "https://cp", "--machine-name", "wsl"])


def test_run_enroll_rejects_loopback_machine_host_for_remote_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Remote enrollment (non-loopback gateway) with a loopback --machine-host is
    refused before any state is written: the gateway would later dial this runner at
    localhost and hit itself, reporting it online under the wrong identity."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)

    def _boom(*_a, **_k):
        raise AssertionError("must reject before fetching config")

    monkeypatch.setattr(enroll.bootstrap, "fetch_bootstrap_config", _boom)  # pyright: ignore[reportUnknownArgumentType]
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://gw.example.com:8000",
            "--machine-name",
            "wsl",
            "--machine-host",
            "127.0.0.1",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 1
    assert "remote enrollment requires a reachable machine-host" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    # Nothing persisted — the rejection is before write_bootstrap_env / the file.
    assert not env_path.exists()
    assert not (tmp_path / "machine_host").exists()


def test_run_enroll_allows_loopback_machine_host_for_loopback_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loopback gateway (single box, gateway co-located) legitimately takes a
    loopback --machine-host — the enroll guard only fires when the gateway is
    remote."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll.bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "x", "AVA_REDIS_URL": "r"},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = enroll.run_enroll(
        [
            "--gateway",
            "http://localhost:8000",
            "--machine-name",
            "box",
            "--machine-host",
            "localhost",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 0
    assert (tmp_path / "machine_host").read_text().strip() == "localhost"


def test_run_enroll_machine_host_survives_reenroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll.bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://x/y", "AVA_REDIS_URL": "redis://x/0"},  # pyright: ignore[reportUnknownArgumentType]
    )
    enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    host_file = tmp_path / "machine_host"
    assert host_file.read_text().strip() == "10.0.0.9"
    # A re-enroll replaces the .env wholesale; the machine_host file is independent.
    enroll.write_bootstrap_env(env_path, gateway="https://cp", machine_name="wsl")
    assert host_file.read_text().strip() == "10.0.0.9"


def test_run_enroll_returns_1_on_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed verification fetch writes NOTHING (fetch-first): an enroll must
    not leave a bootstrap .env behind when the gateway is unreachable or the
    secret is wrong — that .env is what the installed-home gate treats as
    enrolled."""
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", tmp_path / ".env")

    def boom(*_a, **_k):
        raise RuntimeError("401 invalid key")

    monkeypatch.setattr(enroll.bootstrap, "fetch_bootstrap_config", boom)  # pyright: ignore[reportUnknownArgumentType]
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "n",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 1
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "machine_host").exists()


def test_run_enroll_surfaces_gateway_error_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """On an HTTP error from /api/bootstrap (e.g. 401 with a wrong/missing cluster
    secret), enroll prints the gateway's own `detail` instead of the bare httpx
    status line."""
    import httpx

    monkeypatch.setattr(enroll, "AVA_ENV_PATH", tmp_path / ".env")

    def gate(*_a, **_k):
        request = httpx.Request("GET", "https://cp/api/bootstrap")
        response = httpx.Response(
            401,
            request=request,
            json={"detail": "cluster secret required"},
        )
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(enroll.bootstrap, "fetch_bootstrap_config", gate)  # pyright: ignore[reportUnknownArgumentType]
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "n",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "cluster secret required" in err


# ---------------------------------------------------------------------------
# _persist_enroll / run_enroll — the no-cache contract (2026-08-01 refactor)
# ---------------------------------------------------------------------------


def test_persist_enroll_writes_no_cluster_env_vars(tmp_path: Path) -> None:
    """The materialized cluster-facts cache is gone: enroll persists identity and
    health ports only. The fetched payload is a connectivity verification, not a
    source of .env values."""
    home = tmp_path / ".ava"
    enroll._persist_enroll(home, machine_name="host-a")
    env = (home / ".env").read_text()
    assert "AVA_MACHINE_NAME=host-a" in env
    for key in ("AVA_DB_URL", "AVA_REDIS_URL", "AVA_EVENTS_CHANNEL", "AVA_LLM_MODEL"):
        assert key not in env


def test_persist_enroll_writes_the_whole_block_for_a_health_port_base(
    tmp_path: Path,
) -> None:
    """`--health-port-base` writes EVERY daemon's port, derived from the block layout.

    Whole set, because moving some daemons and stranding the rest on the shared
    defaults is worse than moving none — that mixed state is what collided with
    the co-located unit in the first place.

    18112 is a base on the allocator's grid (`18000 + k*16`), the value the docs
    hand operators, so the literals below are the ones an operator would read off
    `ava cluster ls` for a block of the same shape."""
    home = tmp_path / ".ava"
    enroll._persist_enroll(home, machine_name="host-a", health_port_base=18112)
    env = (home / ".env").read_text()
    assert "AVA_AGENT_HOST_HEALTH_PORT=18131" in env  # base + PORT_OFFSETS["agent_host"]
    assert "AVA_OPS_HEALTH_PORT=18119" in env
    assert "AVA_EVENTS_MAINTENANCE_HEALTH_PORT=18126" in env
    for var in health_port_env_aliases().values():
        assert f"{var}=" in env


def test_persist_enroll_leaves_health_ports_unset_without_the_flag(tmp_path: Path) -> None:
    """No base means no keys — each daemon takes its `DEFAULT_PORTS` value.

    An absent key, not a written default: the one-unit-per-machine case must not
    acquire a `.env` line it would then have to keep correct."""
    home = tmp_path / ".ava"
    enroll._persist_enroll(home, machine_name="host-a")
    env = (home / ".env").read_text()
    for var in health_port_env_aliases().values():
        assert var not in env


def test_persist_enroll_creates_home_if_missing(tmp_path: Path) -> None:
    home = tmp_path / "nonexistent" / ".ava"
    enroll._persist_enroll(home, machine_name="host-a")
    assert (home / ".env").exists()


def test_persist_enroll_upserts_existing_env(tmp_path: Path) -> None:
    home = tmp_path / ".ava"
    home.mkdir(parents=True)
    (home / ".env").write_text("AVA_GATEWAY_URL=https://cp\nAVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    enroll._persist_enroll(home, machine_name="host-a")
    env = (home / ".env").read_text()
    # Pre-existing keys are preserved; identity is upserted
    assert "AVA_GATEWAY_URL=https://cp" in env
    assert "AVA_MACHINE_SERVE_AGENT_RUNNER=true" in env
    assert "AVA_MACHINE_NAME=host-a" in env


def test_run_enroll_persists_no_cluster_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full run_enroll integration: gateway fetch succeeds, but only identity is
    persisted — no cluster connection facts are cached in .env."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)

    payload = {
        "AVA_DB_URL": "postgresql://ava@db:5432/ava_prod",
        "AVA_REDIS_URL": "redis://db:6380/0",
        "AVA_EVENTS_CHANNEL": "ava:events",
        "DEEPSEEK_API_KEY": "sk-fetched",
    }
    monkeypatch.setattr(enroll, "_fetch_enroll_payload", lambda *_a, **_k: payload)  # pyright: ignore[reportUnknownArgumentType]

    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 0

    env_text = env_path.read_text()
    assert "AVA_GATEWAY_URL=https://cp" in env_text
    assert "AVA_MACHINE_NAME=wsl" in env_text
    for key in ("AVA_DB_URL", "AVA_REDIS_URL", "AVA_EVENTS_CHANNEL", "DEEPSEEK_API_KEY"):
        assert key not in env_text, f"{key} must not be cached in the runner .env"
    assert not (tmp_path / "cluster").exists()


def test_run_enroll_verifies_connectivity_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch is a real connectivity check: a payload that would leave the
    runner unable to start is still enrolled (the gateway is authoritative; the
    runner's start fails loudly if the gateway is broken)."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_a, **_k: {"AVA_EVENTS_CHANNEL": "ava:events"},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 0
    assert env_path.exists()
    assert "AVA_EVENTS_CHANNEL" not in env_path.read_text()


# ---------------------------------------------------------------------------
# health-port block handling (per-unit fact — never gateway-sourced)
# ---------------------------------------------------------------------------


def _enroll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_args: list[str] | None = None):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_a, **_k: {"AVA_DB_URL": "postgresql://ava@db:5432/ava", "AVA_REDIS_URL": "r"},  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://cp",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
            *(extra_args or []),
        ]
    )
    return rc, env_path


def test_run_enroll_pins_this_units_health_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `--health-port-base` reaches the `.env` the daemons read."""
    rc, env_path = _enroll(tmp_path, monkeypatch, ["--health-port-base", "18112"])
    assert rc == 0
    assert "AVA_AGENT_HOST_HEALTH_PORT=18131" in env_path.read_text()


def test_run_enroll_refuses_an_unusable_health_port_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base that cannot produce a legal block fails BEFORE any state is written."""
    rc, env_path = _enroll(tmp_path, monkeypatch, ["--health-port-base", "65530"])
    assert rc == 1
    assert not env_path.exists()


def test_run_enroll_wsl_auto_defaults_health_port_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag, but this host is WSL2 (issue #1152): a co-located native Windows
    unit would otherwise default to the same shared ports, so enroll applies the
    fixed reserved base instead of leaving the block unset."""
    monkeypatch.setattr(enroll, "IS_WSL", True)
    rc, env_path = _enroll(tmp_path, monkeypatch)
    assert rc == 0
    expected = WSL_DEFAULT_HEALTH_PORT_BASE + 19  # PORT_OFFSETS["agent_host"]
    assert f"AVA_AGENT_HOST_HEALTH_PORT={expected}" in env_path.read_text()


def test_run_enroll_non_wsl_leaves_health_ports_unset_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-default is WSL2-specific: a non-WSL2 host with no flag keeps the
    existing zero-config behaviour (ports unset, each daemon takes its shared
    default) — the single-box and split topologies must not change."""
    monkeypatch.setattr(enroll, "IS_WSL", False)
    rc, env_path = _enroll(tmp_path, monkeypatch)
    assert rc == 0
    for var in health_port_env_aliases().values():
        assert var not in env_path.read_text()


def test_run_enroll_explicit_flag_wins_over_wsl_auto_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-stated base always wins, even on WSL2."""
    monkeypatch.setattr(enroll, "IS_WSL", True)
    rc, env_path = _enroll(tmp_path, monkeypatch, ["--health-port-base", "18112"])
    assert rc == 0
    env = env_path.read_text()
    assert "AVA_AGENT_HOST_HEALTH_PORT=18131" in env
    assert f"AVA_AGENT_HOST_HEALTH_PORT={WSL_DEFAULT_HEALTH_PORT_BASE + 19}" not in env


def test_run_enroll_preserves_existing_health_port_block_on_bare_reenroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare re-enroll must not silently drop a block a prior explicit
    --health-port-base wrote."""
    monkeypatch.setattr(enroll, "IS_WSL", False)
    rc1, env_path = _enroll(tmp_path, monkeypatch, ["--health-port-base", "18112"])
    assert rc1 == 0
    rc2, env_path = _enroll(tmp_path, monkeypatch)  # bare re-enroll, no flag
    assert rc2 == 0
    assert "AVA_AGENT_HOST_HEALTH_PORT=18131" in env_path.read_text()


def test_run_enroll_wsl_reenroll_does_not_clobber_an_explicit_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WSL2 host explicitly given a custom base keeps it on a bare re-enroll."""
    monkeypatch.setattr(enroll, "IS_WSL", True)
    rc1, env_path = _enroll(tmp_path, monkeypatch, ["--health-port-base", "18112"])
    assert rc1 == 0
    rc2, env_path = _enroll(tmp_path, monkeypatch)  # bare re-enroll, still WSL2
    assert rc2 == 0
    env = env_path.read_text()
    assert "AVA_AGENT_HOST_HEALTH_PORT=18131" in env
    assert f"AVA_AGENT_HOST_HEALTH_PORT={WSL_DEFAULT_HEALTH_PORT_BASE + 19}" not in env


def test_run_enroll_wsl_auto_default_is_idempotent_across_reenrolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enroll, "IS_WSL", True)
    rc1, env_path = _enroll(tmp_path, monkeypatch)
    rc2, env_path = _enroll(tmp_path, monkeypatch)
    assert (rc1, rc2) == (0, 0)
    expected = WSL_DEFAULT_HEALTH_PORT_BASE + 19
    assert f"AVA_AGENT_HOST_HEALTH_PORT={expected}" in env_path.read_text()


def test_run_enroll_prints_why_the_wsl_default_was_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(enroll, "IS_WSL", True)
    _enroll(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "auto-defaulted" in out
    assert "#1152" in out
    assert "--health-port-base" in out


# ---------------------------------------------------------------------------
# URL-anchor guard + required enrollment secret (environment or compatibility flag)
# ---------------------------------------------------------------------------


def test_run_enroll_refuses_loopback_data_plane_from_remote_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A remote gateway (non-loopback URL) whose data-plane URLs are loopback is
    misconfigured: the runner would dial ITS OWN loopback and hit itself. The
    anchor model — db/redis hosts derive from the gateway's machine_host — fails
    loud at enroll instead of at first DB connect."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType]
            # username-only URLs: the guard checks the HOST, no credential needed
            # (and a password-shaped fixture would trip the secret scanner)
            "AVA_DB_URL": "postgresql://ava@127.0.0.1:5433/ava",
            "AVA_REDIS_URL": "redis://ava@127.0.0.1:6380/0",
        },
    )
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://gw.example.com:8000",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_MACHINE_HOST" in err and "loopback" in err
    assert not env_path.exists(), "the rejection must write nothing"


def test_run_enroll_accepts_reachable_data_plane_from_remote_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal split deployment: the gateway serves db/redis URLs at its
    reachable address — enroll proceeds."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(
        enroll,
        "_fetch_enroll_payload",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType]
            "AVA_DB_URL": "postgresql://ava@10.0.0.5:5433/ava",
            "AVA_REDIS_URL": "redis://ava@10.0.0.5:6380/0",
        },
    )
    rc = enroll.run_enroll(
        [
            "--gateway",
            "https://10.0.0.5:8000",
            "--machine-name",
            "wsl",
            "--machine-host",
            "10.0.0.9",
            "--cluster-secret",
            "sek",
        ]
    )
    assert rc == 0
    assert env_path.exists()


def test_run_enroll_requires_cluster_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Enrollment requires an explicit secret source: the environment is the
    safe path and --cluster-secret remains only for compatibility."""
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", tmp_path / ".env")
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        enroll.run_enroll(
            ["--gateway", "https://cp", "--machine-name", "n", "--machine-host", "10.0.0.9"]
        )
    assert exc_info.value.code == 2
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_CLUSTER_SECRET" in err
    assert "--cluster-secret" in err
