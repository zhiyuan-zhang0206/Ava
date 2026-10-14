"""Report which Redis tools the cluster instance resolves under the inherited config.

Prints one JSON line with the configured directory, the resolved tool paths and their
`--version` output. Runs in a child whose environment the parent fully controls.
"""

import json
import subprocess
import sys

from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import data_plane_config_from_settings

if __name__ == "__main__":
    instance.is_macos = lambda: False
    config = data_plane_config_from_settings()
    tools = [instance.redis_server_bin(config), instance.redis_cli_bin(config)]
    sys.stdout.write(
        json.dumps(
            {
                "configured": config.redis_bin_dir,
                "tools": tools,
                "versions": [
                    subprocess.check_output([tool, "--version"], text=True).strip()  # noqa: S603
                    for tool in tools
                ],
            }
        )
        + "\n"
    )
