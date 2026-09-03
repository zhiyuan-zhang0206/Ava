"""Actual legacy normal ops entry against native PG; CI scratch only."""

from __future__ import annotations

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psutil
import psycopg
from psycopg import sql


def require(value: bool, reason: str) -> None:  # noqa: FBT001 — explicit CI assertion.
    if not value:
        raise AssertionError(reason)


def main() -> None:  # noqa: PLR0915 — actual service/DB lifetime with strict cleanup.
    home = Path(os.environ["AVA_HOME"]).resolve()
    root = home.parent
    require(os.environ.get("GITHUB_ACTIONS") == "true", "not a GitHub proof")
    require(home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()), "not scratch")
    require(not (root / "source").exists(), "legacy source still exists")
    image = Path(sys.prefix).resolve().parent
    package = Path(sys.prefix) / "lib/python3.12/site-packages"
    require(Path(sys.base_prefix).resolve().is_relative_to(image / "python"), "external Python")
    database = "legacy_" + uuid4().hex
    admin_url = os.environ["AVA_LEGACY_PROOF_PG"]
    parsed = urlsplit(admin_url)
    require(parsed.hostname == "127.0.0.1", "PG fixture is not loopback")
    db_url = urlunsplit(parsed._replace(path="/" + database))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    values = {
        "AVA_DB_URL": db_url,
        "AVA_REDIS_URL": "redis://127.0.0.1:1/0",
        "AVA_CLUSTER_SECRET": "",
        "AVA_MACHINE_NAME": "legacy-proof",
        "AVA_MACHINE_SERVE_GATEWAY": "true",
        "AVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "AVA_MACHINE_HOST": "127.0.0.1",
        "AVA_OPS_HEALTH_PORT": str(port),
        "AVA_TRACE_ENABLED": "false",
        "AVA_TIMEZONE": "UTC",
    }
    env_file = home / ".env"
    env_file.write_text("\n".join(f"{k}={json.dumps(v)}" for k, v in values.items()) + "\n")
    env_file.chmod(0o600)
    (home / "machine_name").write_text("legacy-proof\n")
    poison = home / "plugins/mutable_poison"
    poison.mkdir(parents=True)
    (poison / "plugin.py").write_text("raise RuntimeError('mutable plugin imported')\n")
    # Exact default normal main(), without patching startup/schema/readiness.
    entry = """
import hashlib,json,os,pathlib,psutil,sys,sysconfig
import services.agent_ops.daemon as daemon
from shared import paths,plugins_config
from shared.retained_legacy import external_plugin_read_root
root=pathlib.Path(sys.prefix).resolve()
module=pathlib.Path(daemon.__file__).resolve()
if not module.is_relative_to(root): raise RuntimeError('loaded ops escaped legacy wheel')
if external_plugin_read_root()!=root.parent/'plugins': raise RuntimeError('wrong retained plugin root')
if 'mutable_poison' in plugins_config._discover_plugins(): raise RuntimeError('mutable plugin discovered')
if paths.plugins_dir()!=pathlib.Path(os.environ['AVA_HOME'])/'plugins': raise RuntimeError('installer path changed')
receipt={'pid':os.getpid(),'birth':psutil.Process().create_time(),'module':str(module),
 'executable':psutil.Process().exe(),'base_prefix':sys.base_prefix,
 'stdlib':sysconfig.get_path('stdlib'),'sys_path':sys.path,
 'sql_inventory_sha256':hashlib.sha256((module.parents[2]/'shared/legacy_inventory.json').read_bytes()).hexdigest()}
pathlib.Path(os.environ['AVA_LEGACY_ENTRY_RECEIPT']).write_text(json.dumps(receipt))
daemon.main()
"""
    env = dict(os.environ)
    # Parent environment is explicit private fixture; never inherit external credentials.
    env["AVA_LEGACY_ENTRY_RECEIPT"] = str(root / "normal-entry.json")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def start(label: str, override: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        log = (root / f"normal-{label}.log").open("w")
        try:
            return subprocess.Popen(  # noqa: S603 — exact retained Python/private CI unit.
                [sys.executable, "-I", "-B", "-c", entry],
                cwd=root,
                env=env | (override or {}),
                stdout=log,
                stderr=log,
            )
        finally:
            log.close()

    def stopped(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise AssertionError("normal legacy ops failed bounded graceful stop") from None

    def refused(label: str, expected: str, override: dict[str, str] | None = None) -> None:
        process = start(label, override)
        try:
            require(process.wait(timeout=40) != 0, label + " unexpectedly started")
            require(
                expected in (root / f"normal-{label}.log").read_text(), label + " wrong failure"
            )
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).close()
            except urllib.error.URLError:
                pass
            else:
                raise AssertionError(label + " exposed readiness")
        finally:
            stopped(process)

    process = None
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(db_url, autocommit=True) as conn:
                conn.execute((package / "db/schema.sql").read_text(), prepare=False)  # type: ignore[arg-type]
                for path in sorted((package / "migrations").glob("*.sql")):
                    if path.name.endswith(".down.sql"):
                        continue
                    if conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE name=%s", (path.stem,)
                    ).fetchone():
                        continue
                    with conn.transaction():
                        conn.execute(path.read_text(), prepare=False)  # type: ignore[arg-type]
                        conn.execute("INSERT INTO schema_migrations(name) VALUES(%s)", (path.stem,))
                refused(
                    "wrong-home",
                    "requires explicit canonical AVA_HOME",
                    {"AVA_HOME": str(root / "wrong-home"), "AVA_HOME_OVERRIDE": "1"},
                )
                require(not (root / "wrong-home").exists(), "wrong home had file effects")
                conn.execute(
                    "INSERT INTO schema_migrations(name) VALUES('20990101T000000_unknown')"
                )
                refused("extra-schema", "Schema ahead of code")
                conn.execute("DELETE FROM schema_migrations WHERE name='20990101T000000_unknown'")
                conn.execute("DELETE FROM schema_migrations WHERE name='00000000T000000_baseline'")
                refused("missing-schema", "Schema behind code")
                conn.execute(
                    "INSERT INTO schema_migrations(name) VALUES('00000000T000000_baseline')"
                )
                no_write = """
import json
from shared.migrations import apply_pending_migrations,apply_down,rollback_to
class NoDatabaseAccess:
    def __getattr__(self,name): raise AssertionError('database touched before refusal: '+name)
connection=NoDatabaseAccess()
blocked=[]
for function,args in ((apply_pending_migrations,(connection,)),(apply_down,(connection,'unused')),(rollback_to,(connection,set()))):
    try: function(*args)
    except RuntimeError as exc:
        if 'no migration-write authority' not in str(exc): raise
        blocked.append(function.__name__)
    else: raise AssertionError('packaged migration write permitted')
print(json.dumps(blocked))
"""
                write_probe = subprocess.run(  # noqa: S603 — installed old code, sentinel refuses any DB access.
                    [sys.executable, "-I", "-B", "-c", no_write],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                refused_writes = json.loads(write_probe.stdout)
                require(len(refused_writes) == 3, "not all packaged migration writes refused")
                reader_probe = """
import json
from shared.envelope import wrap_inbound
# Exact source fixture from caller persistence at d39ca01c155305f1e8ae504cf9f5ed1a0e0e8cc1.
legacy=('user','agent:405','system:update','shell:123')
for source in legacy:
    assert 'compatibility probe' in wrap_inbound('compatibility probe',source)
unsupported=('external_agent:codex:run-42','unknown:cli')
for source in unsupported:
    try: wrap_inbound('compatibility probe',source)
    except ValueError as exc:
        if 'Unrecognized inbound source' not in str(exc): raise
    else: raise AssertionError('old reader unexpectedly accepts new source format')
print(json.dumps({'legacySourcesReadable':legacy,'unsupportedSources':unsupported,'rollbackAdmissionProved':False}))
"""
                reader = subprocess.run(  # noqa: S603 — actual installed old reader, no format rewriting.
                    [sys.executable, "-I", "-B", "-c", reader_probe],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                reader_compatibility = json.loads(reader.stdout)
                baseline = package / "db/schema.sql"
                original = baseline.read_bytes()
                baseline.chmod(0o600)
                try:
                    baseline.write_bytes(original + b"\n-- injected SQL corruption\n")
                    refused("sql-tamper", "SQL bytes differ")
                finally:
                    baseline.write_bytes(original)
                    baseline.chmod(0o400)
                migrations = package / "migrations"
                migrations.chmod(0o700)
                extra = migrations / "20990101T000000_untracked.sql"
                try:
                    extra.write_text("SELECT 1;\n")
                    refused("extra-sql", "inventory membership changed")
                finally:
                    extra.unlink(missing_ok=True)
                victim = next(migrations.glob("*.sql"))
                missing = root / "missing-sql.fixture"
                victim.rename(missing)
                try:
                    refused("missing-sql", "inventory membership changed")
                finally:
                    missing.rename(victim)
                    migrations.chmod(0o500)
                process = start("valid")
                until = time.monotonic() + 45
                health = None
                while time.monotonic() < until:
                    if process.poll() is not None:
                        raise AssertionError(
                            "normal legacy ops exited: "
                            + (root / "normal-valid.log").read_text()[-4000:]
                        )
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/healthz", timeout=2
                        ) as response:
                            health = json.load(response)
                        if health["readiness"] == "ok":
                            break
                    except urllib.error.URLError:
                        pass
                    time.sleep(0.1)
                if health is None or health["readiness"] != "ok":
                    raise AssertionError("normal readiness absent")
                require(
                    health["pid"] == process.pid and health["home"] == str(home),
                    "wrong normal responder",
                )
                require(
                    health["sha"] is None, "wheel process_sha falsely impersonates source commit"
                )
                loaded = json.loads((root / "normal-entry.json").read_text())
                native = psutil.Process(process.pid)
                require(
                    loaded["pid"] == native.pid and loaded["birth"] == native.create_time(),
                    "wrong native identity",
                )
                require(
                    Path(loaded["executable"]).resolve().is_relative_to(image),
                    "external executable",
                )
                require(
                    Path(loaded["stdlib"]).resolve().is_relative_to(image / "python"),
                    "external stdlib",
                )
                native_images = sorted(
                    {
                        m.path
                        for m in native.memory_maps(grouped=False)
                        if m.path.startswith("/") and "x" in m.perms
                    }
                )
                require(
                    all(
                        Path(p).is_relative_to(image)
                        or p.startswith(("/usr/lib/", "/lib/", "/lib64/"))
                        for p in native_images
                    ),
                    "normal ops loaded an unretained non-OS native image",
                )
                require(
                    all(
                        not Path(p).is_relative_to(root / "source-hidden")
                        for p in loaded["sys_path"]
                        if p
                    ),
                    "source import path leaked",
                )
                registration = conn.execute(
                    "SELECT home,stopped_at FROM machine_units WHERE machine_name='legacy-proof'"
                ).fetchone()
                require(
                    registration == (str(home), None), "normal boot did not register exact unit"
                )
                stopped(process)
                require(process.poll() is not None, "normal process survived stop")
                result = {
                    "platform": platform.platform(),
                    "architecture": platform.machine(),
                    "normalMain": True,
                    "normalReadiness": True,
                    "nativeRegistration": True,
                    "loaded": loaded,
                    "loadedNativeImages": native_images,
                    "health": health,
                    "wrongHomeRefusedBeforeEffects": True,
                    "extraAppliedRefused": True,
                    "missingAppliedRefused": True,
                    "tamperedSQLRefused": True,
                    "extraSQLRefused": True,
                    "missingSQLRefused": True,
                    "migrationWritesRefusedBeforeDBAccess": refused_writes,
                    "persistedSourceReaderCompatibility": reader_compatibility,
                    "processShaUnknownPreserved": True,
                    "fullRollbackProved": False,
                    "mutablePluginDiscoveryIgnored": True,
                    "installerStillMutableHome": True,
                }
                (root / "cold-boot-proof.json").write_text(json.dumps(result, indent=2) + "\n")
        finally:
            if process is not None:
                stopped(process)
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
