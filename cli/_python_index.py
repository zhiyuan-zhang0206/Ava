"""Read one host package index without changing pip, uv, or repository settings."""

from __future__ import annotations

import configparser
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

PYPI_INDEX = "https://pypi.org/simple"


def _enabled(value: str | None) -> bool:
    if value is None or value.lower() in {"", "0", "false", "no", "off"}:
        return False
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("Invalid boolean setting in Python index configuration")


def _checked_index(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "file"} or (
        parsed.scheme != "file" and not parsed.hostname
    ):
        raise ValueError("The configured Python index must be an HTTP(S) or file URL")
    return value.rstrip("/")


def _uv_config_index(path: Path, *, project: bool = False) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    settings = document.get("tool", {}).get("uv", {}) if project else document
    pip_settings = settings.get("pip", {})
    indexes = settings.get("index", [])
    if len(indexes) > 1 or settings.get("extra-index-url") or pip_settings.get("extra-index-url"):
        raise ValueError(f"Multiple Python indexes are unsupported for locked installs: {path}")
    if indexes:
        if indexes[0].get("default") is not True or indexes[0].get("explicit") is True:
            raise ValueError(
                f"Locked installs require a default index, not an additional/explicit-only index: {path}"
            )
        return str(indexes[0]["url"])
    return (
        pip_settings.get("index-url") or settings.get("default-index") or settings.get("index-url")
    )


def _uv_index(repo: Path, env: Mapping[str, str], home: Path) -> str | None:
    if _enabled(env.get("UV_NO_CONFIG")):
        return None
    if env.get("UV_CONFIG_FILE"):
        path = Path(env["UV_CONFIG_FILE"])
        if not path.is_file():
            raise ValueError("UV_CONFIG_FILE does not name an existing file")
        return _uv_config_index(path)
    local = repo / "uv.toml"
    value = (
        _uv_config_index(local)
        if local.exists()
        else _uv_config_index(repo / "pyproject.toml", project=True)
    )
    if value:
        return value
    if sys.platform == "win32":
        user = Path(env.get("APPDATA", str(home / "AppData" / "Roaming"))) / "uv" / "uv.toml"
        system = Path(env.get("PROGRAMDATA", "C:/ProgramData")) / "uv" / "uv.toml"
    else:
        user = Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "uv" / "uv.toml"
        candidates = [
            Path(p) / "uv" / "uv.toml"
            for p in env.get("XDG_CONFIG_DIRS", "/etc/xdg").split(os.pathsep)
        ] + [Path("/etc/uv/uv.toml")]
        system = next((p for p in candidates if p.is_file()), candidates[-1])
    return _uv_config_index(user) or _uv_config_index(system)


def _pip_paths(repo: Path, env: Mapping[str, str], home: Path) -> list[Path]:
    explicit = env.get("PIP_CONFIG_FILE")
    if explicit and Path(explicit) == Path(os.devnull):
        return []
    if sys.platform == "win32":
        name = "pip.ini"
        global_paths = [Path(env.get("PROGRAMDATA", "C:/ProgramData")) / "pip" / name]
        user_paths = [
            home / "pip" / name,
            Path(env.get("APPDATA", str(home / "AppData" / "Roaming"))) / "pip" / name,
        ]
    elif sys.platform == "darwin":
        name = "pip.conf"
        global_paths = [Path("/Library/Application Support/pip/pip.conf")]
        user_dir = home / "Library" / "Application Support" / "pip"
        if not user_dir.is_dir():
            user_dir = Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pip"
        user_paths = [home / ".pip" / name, user_dir / name]
    else:
        name = "pip.conf"
        global_paths = [
            Path(p) / "pip" / name for p in env.get("XDG_CONFIG_DIRS", "/etc/xdg").split(os.pathsep)
        ] + [Path("/etc/pip.conf")]
        user_paths = [
            home / ".pip" / name,
            Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pip" / name,
        ]
    if explicit and Path(explicit).is_file():
        user_paths = []
    return (
        global_paths + user_paths + [repo / ".venv" / name] + ([Path(explicit)] if explicit else [])
    )


def _pip_index(repo: Path, env: Mapping[str, str], home: Path) -> str | None:
    if env.get("PIP_EXTRA_INDEX_URL") or _enabled(env.get("PIP_NO_INDEX")):
        raise ValueError(
            "Locked installs require one Python index; PIP_EXTRA_INDEX_URL/PIP_NO_INDEX are unsupported"
        )
    if env.get("PIP_INDEX_URL"):
        return env["PIP_INDEX_URL"]
    config = configparser.RawConfigParser()
    try:
        config.read(_pip_paths(repo, env, home))
    except configparser.Error:
        raise ValueError("Cannot parse the machine pip configuration") from None
    for section in ("install", "global"):
        if config.get(section, "extra-index-url", fallback="") or _enabled(
            config.get(section, "no-index", fallback=None)
        ):
            raise ValueError(
                "Locked installs require one Python index; pip extra-index-url/no-index are unsupported"
            )
    return config.get("install", "index-url", fallback=None) or config.get(
        "global", "index-url", fallback=None
    )


def python_index(repo: Path, env: Mapping[str, str]) -> str:
    """Use explicit uv settings, then pip settings, then canonical PyPI.

    An install's explicit mirror profile already supplies UV_DEFAULT_INDEX.
    This only bridges pip's single-index setting, which uv does not read itself.
    Index values stay in child environment variables, never command arguments.
    """
    if env.get("UV_INDEX") or env.get("UV_EXTRA_INDEX_URL") or _enabled(env.get("UV_NO_INDEX")):
        raise ValueError("Locked installs require one default Python index; use UV_DEFAULT_INDEX")
    home = Path(env.get("HOME", str(Path.home())))
    value = env.get("UV_DEFAULT_INDEX") or env.get("UV_INDEX_URL")
    return _checked_index(
        value or _uv_index(repo, env, home) or _pip_index(repo, env, home) or PYPI_INDEX
    )
