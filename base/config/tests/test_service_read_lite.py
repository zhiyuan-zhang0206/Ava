"""The SDK's explicit configuration owner stays off the eager model stack."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _probe(code: str, home: Path, *argv: str, profile: str | None = None) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(home)
    if profile is not None:
        env["AVA_PROCESS_PROFILE"] = profile
    proc = subprocess.run(  # noqa: S603 — this interpreter runs the fixed import probe
        [sys.executable, "-B", "-c", code, *argv],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_explicit_config_authority_import_stays_lite(tmp_path: Path) -> None:
    home = tmp_path / "boot-home"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/test\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
    )
    code = (
        "import json, sys, base.config as c\n"
        "import base.config.service_read\n"
        "print(json.dumps({'boot': c._boot_state(),\n"
        " 'pydantic_settings': 'pydantic_settings' in sys.modules,\n"
        " 'config_modules': sorted(m for m in sys.modules if m.startswith('base.config'))}))\n"
    )
    result = _probe(code, home)
    assert result["boot"]["mode"] == "lite"
    assert result["boot"]["upgrades"] == 0
    assert result["pydantic_settings"] is False
    assert result["config_modules"] == [
        "base.config",
        "base.config._lite",
        "base.config.profiles",
        "base.config.service_read",
    ]


def test_sdk_root_complete_read_keeps_first_use_environment_and_fixed_file(tmp_path: Path) -> None:
    home, later_home = tmp_path / "root-home", tmp_path / "later-home"
    home.mkdir()
    later_home.mkdir()
    path = home / ".env"
    path.write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/test\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "AVA_TRACE_ENABLED=false\n"
    )
    (later_home / ".env").write_text("AVA_TRACE_ENABLED=true\n")
    code = (
        "import json, os, sys, base.config as c, ava\n"
        "os.environ['AVA_WEB_JINA_BASE_URL'] = 'https://early.invalid/'\n"
        "ava.ensure_plugins_loaded(surface=True)\n"
        "from ava.sdk_surface.install import installed\n"
        "owner = installed().authority\n"
        "assert owner.runtime is c.settings\n"
        "assert not owner.runtime.has_domain('web')\n"
        "before = c._boot_state()\n"
        "os.environ['AVA_WEB_JINA_BASE_URL'] = 'https://first-use.invalid/'\n"
        "os.environ['AVA_HOME'] = sys.argv[1]\n"
        "first = owner.service_field_value('web_jina_reader_base')\n"
        "os.environ['AVA_WEB_JINA_BASE_URL'] = 'https://after-read.invalid/'\n"
        "second = owner.service_field_value('web_jina_reader_base')\n"
        "aliases = owner.read_env_aliases()\n"
        "print(json.dumps({'before': before, 'first': first, 'second': second,\n"
        " 'path': str(owner.env_path), 'trace': aliases['AVA_TRACE_ENABLED']}))\n"
    )
    result = _probe(code, home, str(later_home), profile="runner")
    assert result["before"]["mode"] == "lite"
    assert result["before"]["upgrades"] == 0
    assert result["first"] == "https://first-use.invalid/"
    assert result["second"] == result["first"]
    assert result["path"] == str(path)
    assert result["trace"] == "false"
