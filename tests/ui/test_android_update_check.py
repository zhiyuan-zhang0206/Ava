"""Execute the Android update selector against out-of-order release lists."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "ui" / "app" / "src-tauri" / "scripts" / "update-check.js"

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const releases = JSON.parse(process.argv[1]);
const appended = [];

function element() {
  return {
    children: [],
    style: {},
    textContent: "",
    setAttribute() {},
    addEventListener() {},
    appendChild(child) { this.children.push(child); },
    remove() {},
  };
}

const body = element();
body.appendChild = function (child) { appended.push(child); };
const window = {
  __AVA_SHELL__: {
    releasesApi: "https://api.github.test/releases",
    version: "0.4.0",
    notifications: false,
  },
  __TAURI_INTERNALS__: { invoke: async () => {} },
  sessionStorage: {
    getItem() { return null; },
    setItem() {},
  },
};
window.top = window;
const context = {
  window,
  document: { body, createElement: element },
  fetch: async () => ({ ok: true, json: async () => releases }),
  console,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
setTimeout(() => {
  const banner = appended[0];
  process.stdout.write(banner ? banner.children[0].textContent : "");
}, 0);
"""


def _release(
    version: str,
    *,
    apk: bool = True,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    assets = []
    if apk:
        assets.append(
            {
                "name": f"Ava_{version}.apk",
                "browser_download_url": f"https://downloads.test/Ava_{version}.apk",
            }
        )
    return {
        "tag_name": f"shell-v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }


def _selected_banner(releases: list[dict[str, object]]) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed Node harness, no shell
        ["node", "-e", _HARNESS, json.dumps(releases), str(_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        ([_release("0.3.0"), _release("0.5.0")], "Ava 0.5.0 is available"),
        ([_release("0.6.0", apk=False), _release("0.5.0")], "Ava 0.5.0 is available"),
        ([_release("0.5.0"), _release("0.7.0")], "Ava 0.7.0 is available"),
        (
            [_release("0.9.0", prerelease=True), _release("0.8.0", draft=True)],
            "",
        ),
    ],
)
def test_selects_highest_published_semver_with_an_apk(
    releases: list[dict[str, object]], expected: str
) -> None:
    assert _selected_banner(releases) == expected
