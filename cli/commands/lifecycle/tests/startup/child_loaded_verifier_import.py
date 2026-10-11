"""Fresh-interpreter check that the loaded-source verifier imports without Settings."""

import importlib.abc
import sys


class Poison(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname in {"base.config", "base.paths", "base.host.env.dotenv_boot"}:
            raise AssertionError("Settings preload: " + fullname)


if __name__ == "__main__":
    sys.meta_path.insert(0, Poison())
    import base.deploy.release.runtime_interpreter  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "base.config" not in sys.modules
