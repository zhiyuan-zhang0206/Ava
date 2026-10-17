from __future__ import annotations

import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "ui" / "app" / "src-tauri" / "scripts" / "update-check.js"

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const scenario = JSON.parse(process.argv[1]);

const appended = [];
const invocations = [];

function element() {
  return {
    children: [],
    handlers: {},
    style: {},
    textContent: "",
    disabled: false,
    removed: false,
    setAttribute() {},
    addEventListener(type, handler) {
      this.handlers[type] = handler;
    },
    appendChild(child) {
      this.children.push(child);
    },
    remove() {
      this.removed = true;
    },
  };
}

const body = element();
body.appendChild = function (child) {
  appended.push(child);
};

const window = {
  __MEIVA_APP__: scenario.config,
  __TAURI_INTERNALS__: {
    invoke: async function (command, args) {
      invocations.push({ command: command, args: args });
      if (command === "app_check_update") return scenario.check;
      if (command === "app_install_update") return scenario.install;
      return {};
    },
  },
  sessionStorage: {
    getItem() {
      return null;
    },
    setItem() {},
  },
};
window.top = window;

vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {
  window: window,
  document: { body: body, createElement: element },
  location: { protocol: "https:", hostname: "console.test" },
  console: { error() {}, log() {} },
});

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

(async function () {
  await tick();
  const banner = appended[0] || null;
  const result = {
    banner: banner !== null,
    text: banner ? banner.children[0].textContent : "",
    actions: banner ? banner.children[1].children.map((child) => child.textContent) : [],
    commands: invocations.map((call) => call.command),
    notifications: invocations
      .filter((call) => call.command === "app_notify")
      .map((call) => call.args),
  };
  if (banner && scenario.interact) {
    const update = banner.children[1].children[0];
    update.handlers.click();
    await tick();
    result.afterInstall = update.textContent;
    result.removed = banner.removed;
    if (!banner.removed) {
      update.handlers.click();
      await tick();
      result.downloadPage = invocations
        .filter((call) => call.command === "app_open_external")
        .map((call) => call.args.url)[0];
    }
  }
  process.stdout.write(JSON.stringify(result));
})();
"""

_DOWNLOAD_PAGE = "https://github.com/zhiyuan-zhang0206/Meiva/releases/tag/android-latest"


def _available(version_name: str = "1.0.0", version_code: int = 4003) -> dict[str, object]:
    return {
        "status": "available",
        "reason": None,
        "currentVersionCode": 4002,
        "latestVersionName": version_name,
        "latestVersionCode": version_code,
        "downloadPage": _DOWNLOAD_PAGE,
    }


def _settled(status: str) -> dict[str, object]:
    return {
        "status": status,
        "reason": None if status == "current" else "network",
        "currentVersionCode": 4002,
        "latestVersionName": None,
        "latestVersionCode": None,
        "downloadPage": _DOWNLOAD_PAGE,
    }


def _run(
    *,
    check: dict[str, object] | None = None,
    install: dict[str, object] | None = None,
    notifications: bool = False,
    interact: bool = False,
    platform: str = "android",
) -> dict[str, object]:
    # meiva:note
    # Each Android update prompt case executes the checked-in injected script with canned native command results and reads only the elements it appended.
    # meiva:link tests/docs/tests.meiva.okf.md
    scenario = {
        "config": {
            "platform": platform,
            "version": "0.4.2",
            "notifications": notifications,
        },
        "check": check,
        "install": install,
        "interact": interact,
    }
    result = subprocess.run(  # noqa: S603
        ["node", "-e", _HARNESS, json.dumps(scenario), str(_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_offers_a_newer_build_with_an_install_action() -> None:
    result = _run(check=_available())
    assert result["banner"] is True
    assert result["text"] == "Meiva 1.0.0 (build 4003) is available"
    assert result["actions"] == ["Update", "Later"]
    assert result["commands"] == ["app_check_update"]


def test_notifies_only_when_the_setting_is_on() -> None:
    silent = _run(check=_available())
    assert silent["notifications"] == []

    announced = _run(check=_available(), notifications=True)
    assert announced["notifications"] == [
        {
            "title": "Meiva 1.0.0 (build 4003) is available",
            "body": "Tap Update in the app to install it.",
        }
    ]


def test_stays_silent_without_a_newer_build_or_a_usable_answer() -> None:
    for check in (_settled("current"), _settled("failed"), _settled("unsupported")):
        result = _run(check=check)
        assert result["banner"] is False
        assert result["notifications"] == []


def test_desktop_never_runs_the_android_check() -> None:
    result = _run(check=_available(), platform="macos")
    assert result["commands"] == []
    assert result["banner"] is False


def test_a_failed_install_falls_back_to_the_download_page() -> None:
    result = _run(
        check=_available(),
        install={"status": "failed", "reason": "network", "downloadPage": _DOWNLOAD_PAGE},
        interact=True,
    )
    assert result["afterInstall"] == "Open download page"
    assert result["removed"] is False
    assert result["downloadPage"] == _DOWNLOAD_PAGE


def test_an_opened_installer_removes_the_bar() -> None:
    result = _run(
        check=_available(),
        install={"status": "installer-opened", "reason": None, "downloadPage": _DOWNLOAD_PAGE},
        interact=True,
    )
    assert result["removed"] is True
