"""Build the explicit, non-production fixture for cold plugin closure CI."""

import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) / "runtime_fixture"
root.mkdir(parents=True)
(root / "ava-plugin.json").write_text(
    json.dumps(
        {
            "apiVersion": 2,
            "name": "runtime_fixture",
            "version": "1.0.0",
            "engines": {"ava": ">=0.1.0"},
            "dependencies": {"pythonPackages": ["pydantic>=2,<3"]},
        }
    )
)
(root / "plugin.py").write_text("from .sibling import VALUE\n")
(root / "sibling.py").write_text(
    "from pathlib import Path\nVALUE = (Path(__file__).parent / 'resource.txt').read_text()\n"
)
(root / "resource.txt").write_text("retained-resource")
(root / "setup.py").write_text("def scaffold(home):\n    return home / 'runtime-fixture-config'\n")
(root / "services.py").write_text(
    "from ops.spec import ServiceSpec\n"
    "def services():\n"
    "    return (ServiceSpec(session='runtime-fixture', cmd='true', "
    "capabilities=frozenset({'agent-runner'}), requires_db=False),)\n"
)
