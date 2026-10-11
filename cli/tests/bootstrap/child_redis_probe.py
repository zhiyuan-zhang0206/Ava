"""Report which Redis tools the cluster instance resolves under the inherited config.

Prints one JSON line with the configured directory, the resolved tool paths and their
`--version` output. Runs in a child whose environment the parent fully controls.
"""

import json
import subprocess
import sys

from base.config import settings
from cli.commands.data_plane import cluster_instance as instance

if __name__ == "__main__":
    instance.is_macos = lambda: False
    tools = [instance._redis_server_bin(), instance.redis_cli_bin()]
    sys.stdout.write(
        json.dumps(
            {
                "configured": settings.data_plane.redis_bin_dir,
                "tools": tools,
                "versions": [
                    subprocess.check_output([tool, "--version"], text=True).strip()  # noqa: S603
                    for tool in tools
                ],
            }
        )
        + "\n"
    )
