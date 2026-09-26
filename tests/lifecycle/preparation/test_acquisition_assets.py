"""Asset wrapper authority and captured-source command boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.release_prepare import acquisition_assets as assets
from cli.release_prepare.acquisition_dependencies import file_input, tree_input
from cli.release_prepare.acquisition_models import FrontendTools
from cli.release_prepare.acquisition_process import Commands
from shared.runtime_release import ReleaseRejectedError


class FrontendCommands(Commands):
    node_version = "v22.23.2"

    def __init__(self, work: Path, uv: Path) -> None:
        super().__init__(work, uv)
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int = 900,
        environment: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(argv)
        if argv[-1] == "--version":
            return self.node_version
        assert environment is not None
        assert environment["AVA_FRONTEND_RELEASE"] == "1"
        assert environment["NEXT_PUBLIC_GATEWAY_PORT"] == "18203"
        assert environment["NEXT_PUBLIC_API_BASE"] == ""
        if "--input-type=module" in argv:
            assert Path(argv[-4]) == cwd / "scripts/prepare_frontend_release.mjs"
            assert Path(argv[-3]) == cwd / "ui/web"
            output = Path(argv[-2])
            output.mkdir()
            (output / "fixture").write_text("controlled asset output")
        else:
            assert cwd.name == "web"
            assert Path(argv[1]) == self.work / "node/npm/bin/npm-cli.js"
        return ""


@pytest.fixture
def frontend_inputs(tmp_path: Path) -> tuple[FrontendCommands, Path, FrontendTools]:
    root = tmp_path.resolve()
    source = root / "captured"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/runtime-node-version").write_text("22.23.2\n")
    (source / "ui/web").mkdir(parents=True)
    (source / "ui/web/package-lock.json").write_text("{}\n")
    node = root / "node"
    node.write_bytes(b"explicit supplied Node")
    npm = root / "npm"
    (npm / "bin").mkdir(parents=True)
    (npm / "bin/npm-cli.js").write_text("// controlled npm input\n")
    work = root / "work"
    work.mkdir()
    return (
        FrontendCommands(work, node),
        source,
        FrontendTools(
            node=file_input(node),
            npm=tree_input(npm),
            gateway_port=18203,
        ),
    )


def test_frontend_uses_captured_lock_and_builder_with_private_tools(
    frontend_inputs: tuple[FrontendCommands, Path, FrontendTools],
) -> None:
    commands, source, tools = frontend_inputs
    before = (source / "ui/web/package-lock.json").read_bytes()
    result = assets.frontend(commands, source, tools)
    assert result.root == commands.work / "frontend"
    assert commands.calls[1][-1] == "ci"
    assert commands.calls[2][-2:] == ["run", "build"]
    assert all(call[0] == str(commands.work / "node/bin/node") for call in commands.calls)
    assert (source / "ui/web/package-lock.json").read_bytes() == before


def test_node_patch_mismatch_refuses_before_npm_or_frontend_build(
    frontend_inputs: tuple[FrontendCommands, Path, FrontendTools],
) -> None:
    commands, source, tools = frontend_inputs
    commands.node_version = "v22.23.3"
    with pytest.raises(ReleaseRejectedError, match="captured source pin"):
        assets.frontend(commands, source, tools)
    assert len(commands.calls) == 1
    assert not (commands.work / "frontend").exists()


def test_supplied_node_change_refuses_before_execution(
    frontend_inputs: tuple[FrontendCommands, Path, FrontendTools],
) -> None:
    commands, source, tools = frontend_inputs
    tools.node.path.write_bytes(b"changed")
    with pytest.raises(ReleaseRejectedError, match="tool bytes changed"):
        assets.frontend(commands, source, tools)
    assert commands.calls == []
