"""Fresh processes bind the winning whole image after first-boot contention."""

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_revision,
    write_config_image,
)
from base.packages.plugins import config_registration
from base.packages.plugins.config_registration import (
    InvalidConfigData,
    SchemaDriftError,
    bind_plugin_config,
    disk_image_path,
    get_plugin_config,
)


class BootConfig(BaseModel):
    marker: str = "default"


async def _release_creation(
    children: list[asyncio.subprocess.Process],
) -> list[tuple[bytes, bytes]]:
    for child in children:
        assert child.stdin is not None
        child.stdin.write(b"CREATE\n")
        await child.stdin.drain()
    return await asyncio.wait_for(asyncio.gather(*(child.communicate() for child in children)), 30)


async def test_fresh_processes_bind_the_same_winning_image(unit_home: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from unittest.mock import patch
        from pydantic import BaseModel
        from base.host.env.agent_slices import AgentSlices
        from base.packages.plugins import config_registration as registration

        class Config(BaseModel):
            marker: str = sys.argv[1]

        original = registration.write_default_disk_image
        def synchronized_write(plugin, cls):
            print('READY', flush=True)
            assert sys.stdin.readline() == 'CREATE\\n'
            return original(plugin, cls)

        with patch.object(registration, 'write_default_disk_image', synchronized_write):
            registration.bind_plugin_config('boot-race', Config)
        bound = registration.get_plugin_config('boot-race', AgentSlices.resolve(), Config)
        print(bound.marker, flush=True)
        """
    )
    children: list[asyncio.subprocess.Process] = []
    try:
        for marker in ("first", "second"):
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                script,
                marker,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            children.append(child)
        for child in children:
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readline(), 30) == b"READY\n"
        assert not disk_image_path("boot-race").exists()
        results = await _release_creation(children)
        for child, (_, stderr) in zip(children, results, strict=True):
            assert child.returncode == 0, stderr.decode()
        winner = json.loads(disk_image_path("boot-race").read_text())["marker"]
        assert winner in ("first", "second")
        assert [stdout.decode().strip() for stdout, _ in results] == [winner, winner]
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
                await child.wait()


def test_bind_rereads_image_created_after_the_missing_read(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = config_registration.read_plugin_config
    image = disk_image_path("read-race")

    def competing_read(plugin: str, cls: type[BaseModel], path: Path) -> BaseModel:
        instance = reader(plugin, cls, path)
        if not path.exists():
            write_config_image(
                PluginConfigOwner(plugin, cls, path),
                BootConfig(marker="winner"),
                expected_digest=image_revision(None),
            )
        return instance

    monkeypatch.setattr(config_registration, "read_plugin_config", competing_read)
    undo = bind_plugin_config("read-race", BootConfig)
    try:
        assert get_plugin_config("read-race", AgentSlices.resolve(), BootConfig).marker == "winner"
        assert json.loads(image.read_text()) == {"marker": "winner"}
    finally:
        undo()


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("", InvalidConfigData),
        ("invalid-json", InvalidConfigData),
        ('{"unexpected": 1}', SchemaDriftError),
        ('{"marker": 123}', InvalidConfigData),
    ],
)
def test_bind_rejects_invalid_winning_image(
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_error: type[Exception],
) -> None:
    original = config_registration.write_default_disk_image
    image = disk_image_path("invalid-winner")

    def competing_write(plugin: str, cls: type[BaseModel]) -> Path:
        image.parent.mkdir(parents=True)
        image.write_text(content)
        return original(plugin, cls)

    monkeypatch.setattr(config_registration, "write_default_disk_image", competing_write)
    with pytest.raises(expected_error):
        bind_plugin_config("invalid-winner", BootConfig)
    with pytest.raises(KeyError):
        get_plugin_config("invalid-winner", AgentSlices.resolve(), BootConfig)
    assert image.read_text() == content


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_bind_propagates_unknown_creation_errors(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    error = error_type("unexpected create failure")

    def fail_write(plugin: str, cls: type[BaseModel]) -> Path:
        raise error

    monkeypatch.setattr(config_registration, "write_default_disk_image", fail_write)
    with pytest.raises(error_type) as captured:
        bind_plugin_config("unknown-writer-error", BootConfig)
    assert captured.value is error
    assert not disk_image_path("unknown-writer-error").exists()
    with pytest.raises(KeyError):
        get_plugin_config("unknown-writer-error", AgentSlices.resolve(), BootConfig)


def test_default_writer_still_rejects_an_existing_image(unit_home: Path) -> None:
    image = disk_image_path("create-only")
    write_config_image(
        PluginConfigOwner("create-only", BootConfig, image),
        BootConfig(marker="existing"),
        expected_digest=image_revision(None),
    )
    before = image.read_bytes()
    with pytest.raises(PluginConfigChangedError):
        config_registration.write_default_disk_image("create-only", BootConfig)
    assert image.read_bytes() == before


def test_bind_does_not_replace_a_disappearing_winner_with_defaults(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = disk_image_path("missing-winner")

    def disappearing_write(plugin: str, cls: type[BaseModel]) -> Path:
        image.parent.mkdir(parents=True)
        image.write_text(BootConfig(marker="winner").model_dump_json())
        image.unlink()
        raise PluginConfigChangedError("winning image disappeared")

    monkeypatch.setattr(config_registration, "write_default_disk_image", disappearing_write)
    with pytest.raises(FileNotFoundError):
        bind_plugin_config("missing-winner", BootConfig)
    assert not image.exists()
    with pytest.raises(KeyError):
        get_plugin_config("missing-winner", AgentSlices.resolve(), BootConfig)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_bind_propagates_unknown_winning_image_errors(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    parser = config_registration.config_from_image
    error = error_type("unexpected winning image failure")
    image = disk_image_path("unknown-read-error")

    def fail_read(cls: type[BaseModel], content: str, path: Path) -> BaseModel:
        if path == image:
            raise error
        return parser(cls, content, path)

    monkeypatch.setattr(config_registration, "config_from_image", fail_read)
    with pytest.raises(error_type) as captured:
        bind_plugin_config("unknown-read-error", BootConfig)
    assert captured.value is error
    assert json.loads(image.read_text()) == {"marker": "default"}
    with pytest.raises(KeyError):
        get_plugin_config("unknown-read-error", AgentSlices.resolve(), BootConfig)
