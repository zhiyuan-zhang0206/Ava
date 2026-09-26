"""Invoke the captured frontend and collector preparation APIs without a home."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cli.release_prepare.acquisition_dependencies import tree_input
from cli.release_prepare.acquisition_models import FrontendTools
from cli.release_prepare.acquisition_process import Commands
from cli.release_prepare.models import TreeInput
from shared.runtime_prepare import inventory_digest, tree_inventory
from shared.runtime_release import ReleaseRejectedError, file_sha256


def validate_frontend_tools(tools: FrontendTools) -> None:
    if file_sha256(tools.node.path) != tools.node.digest or tree_input(tools.npm.root) != tools.npm:
        raise ReleaseRejectedError("supplied frontend tool bytes changed")


def frontend(commands: Commands, source: Path, tools: FrontendTools) -> TreeInput:
    validate_frontend_tools(tools)
    pinned = "v" + (source / "scripts/runtime-node-version").read_text().strip()
    node = commands.work / "node/bin/node"
    node.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(tools.node.path, node)
    if file_sha256(node) != tools.node.digest:
        raise ReleaseRejectedError("Node changed while privately copying")
    node.chmod(0o700)
    if commands.run([str(node), "--version"], source) != pinned:
        raise ReleaseRejectedError("Node differs from the captured source pin")
    npm = commands.work / "node/npm"
    shutil.copytree(tools.npm.root, npm)
    if inventory_digest(tree_inventory(npm)) != tools.npm.digest:
        raise ReleaseRejectedError("bundled npm changed while privately copying")
    app = source / "ui/web"
    locked = file_sha256(app / "package-lock.json")
    environment = {
        "PATH": str(node.parent) + os.pathsep + commands.environment["PATH"],
        "AVA_FRONTEND_RELEASE": "1",
        "NEXT_PUBLIC_GATEWAY_PORT": str(tools.gateway_port),
        "NEXT_PUBLIC_API_BASE": "",
    }
    for arguments in (("ci",), ("run", "build")):
        commands.run(
            [str(node), str(npm / "bin/npm-cli.js"), *arguments],
            app,
            environment=environment,
            timeout=1800,
        )
    if file_sha256(app / "package-lock.json") != locked:
        raise ReleaseRejectedError("frontend build changed the captured npm lock")
    target = commands.work / "frontend"
    script = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [modulePath, source, target, port] = process.argv.slice(1);
const { frontendInputs, prepareFrontend, recordFrontendBuildConfig } = await import(pathToFileURL(modulePath));
recordFrontendBuildConfig(source, port, '');
prepareFrontend(source, fs.realpathSync(process.execPath), target, frontendInputs(source, process.execPath));
"""
    commands.run(
        [
            str(node),
            "--input-type=module",
            "-e",
            script,
            str(source / "scripts/prepare_frontend_release.mjs"),
            str(app),
            str(target),
            str(tools.gateway_port),
        ],
        source,
        environment=environment,
    )
    return tree_input(target)


def collector(commands: Commands, source: Path, python: Path) -> TreeInput:
    target = commands.work / "collector"
    # The captured script shares the settings-free canonical downloader with converge.
    script = """
import importlib.abc, runpy, sys
class DenyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('shared.config', 'shared.db', 'cli.commands', 'services.')):
            raise RuntimeError('collector preparation imported runtime authority: ' + fullname)
sys.meta_path.insert(0, DenyRuntime())
sys.path.insert(0, sys.argv[1])
script, destination = sys.argv[2:]
sys.argv = [script, destination]
runpy.run_path(script, run_name='__main__')
"""
    commands.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            script,
            str(source),
            str(source / "scripts/prepare_otel_release.py"),
            str(target),
        ],
        source,
        timeout=1900,
    )
    return tree_input(target)
