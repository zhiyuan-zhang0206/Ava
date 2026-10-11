"""Child process for test_tests_location.py: run the registered-path checks, then report imports.

Usage: ``child_registered_checks.py REPO_ROOT FIXTURE_REPO REGISTERED_JSON``. It runs
`tests_location.main` over the fixture repository with the registered tests allowed,
then exits non-zero when the run loaded the placement rule or the suggestion module:
a hook run is a path lookup and must not pay for either.
"""

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
