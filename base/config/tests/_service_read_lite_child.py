"""Lite-boot probes of the explicit configuration authority; never imported by a production entry.

Run as ``python -B _service_read_lite_child.py <scenario> [later-home]`` from the repository root.
The last stdout line is the JSON observation checked by the parent.
"""

from __future__ import annotations

import json
import os
import sys


def import_stays_lite() -> None:
    import base.config as c
    import base.config.service_read  # noqa: F401  # pyright: ignore[reportUnusedImport]

    sys.stdout.write(
        json.dumps(
            {
                "boot": c._boot_state(),
                "pydantic_settings": "pydantic_settings" in sys.modules,
                "config_modules": sorted(m for m in sys.modules if m.startswith("base.config")),
            }
        )
        + "\n"
    )


def root_complete_read(later_home: str) -> None:
    import ava
    import base.config as c

    os.environ["AVA_WEB_JINA_BASE_URL"] = "https://early.invalid/"
    boot = c.ConfigBoot()
    boot.boot()
    ava.ensure_plugins_loaded(surface=True, config=boot)
    from ava.sdk_surface.install import installed

    installation = installed()
    assert installation is not None
    owner = installation.authority
    assert owner is not None
    assert owner.runtime is boot.view
    assert not owner.runtime.has_domain("web")
    before = boot.boot_state()
    os.environ["AVA_WEB_JINA_BASE_URL"] = "https://first-use.invalid/"
    os.environ["AVA_HOME"] = later_home
    first = owner.service_field_value("web_jina_reader_base")
    os.environ["AVA_WEB_JINA_BASE_URL"] = "https://after-read.invalid/"
    second = owner.service_field_value("web_jina_reader_base")
    aliases = owner.read_env_aliases()
    sys.stdout.write(
        json.dumps(
            {
                "before": before,
                "first": first,
                "second": second,
                "path": str(owner.env_path),
                "trace": aliases["AVA_TRACE_ENABLED"],
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1] == "import_stays_lite":
        import_stays_lite()
    else:
        root_complete_read(sys.argv[2])
