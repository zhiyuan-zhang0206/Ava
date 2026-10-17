"""`import base.cluster` stays free of Settings and the home.

The restricted restore worker imports the cluster package before either exists.
The typed MachineIdentity value owner stays cold; its ConfigBoot factory does not
enter until the real composition root requests it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cluster_package_modules_do_not_load_settings_or_create_the_home(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    subprocess.run(  # noqa: S603 -- isolated local import boundary, fixed child script and captured repo.
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(repo)],
        check=True,
        env={**os.environ, "MEIVA_HOME": str(tmp_path / "never-created")},
    )


def _cluster_package_modules() -> None:
    """The child: import the cluster modules in a fresh interpreter and inspect what loaded."""
    import base.cluster  # pyright: ignore[reportUnusedImport]
    import base.cluster.ownership  # pyright: ignore[reportUnusedImport]
    import base.cluster.port_preflight  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "base.cluster.machine" in sys.modules
    loaded = [n for n in ("base.config", "base.cluster.machine.boot") if n in sys.modules]
    assert not loaded, loaded
    # `base.paths` may load (the private-directory primitive lives there), but the home it
    # names must be neither resolved into existence nor created by the import.
    home = os.environ["MEIVA_HOME"]
    assert not Path(home).exists(), f"importing the cluster package created the home {home}"


if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])
    _cluster_package_modules()
