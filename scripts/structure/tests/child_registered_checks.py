from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from scripts.structure import tests_location as tl

_NEVER_LOADED_PREFIX = "scripts.structure.placement"
_NEVER_LOADED = "scripts.structure.tests_location_suggest"

allowed = {path: tuple(entry) for path, entry in json.loads(sys.argv[3]).items()}
status = tl.main([], repo_root=Path(sys.argv[2]), allowed=allowed)
assert status == 0
loaded = [
    name for name in sys.modules if name.startswith(_NEVER_LOADED_PREFIX) or name == _NEVER_LOADED
]
assert not loaded, loaded
