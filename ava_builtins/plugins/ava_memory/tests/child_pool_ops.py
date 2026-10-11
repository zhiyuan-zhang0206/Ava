"""Child process for the `pool_ops` home tests: a from-scratch interpreter importing `pool_ops`.

Usage: `child_pool_ops.py <pool_dir|machine_name|import_side_effects>`; prints the result.
"""

import sys

before = set(sys.modules)
from ava_builtins.plugins.ava_memory import pool_ops  # noqa: E402

after = set(sys.modules)

fn = sys.argv[1]
if fn == "pool_dir":
    print(str(pool_ops.pool_dir()))
elif fn == "machine_name":
    print(pool_ops.machine_name())
elif fn == "import_side_effects":
    # Nothing beyond pool_ops itself and its own (stdlib) imports should load as a side effect --
    # in particular, no sibling submodule that does plugin/hook declaration (plugin.py, sdk.py,
    # services.py, setup.py, ...).
    print(
        ",".join(
            sorted(m for m in after - before if m.startswith("ava_builtins.plugins.ava_memory"))
        )
    )
else:
    raise ValueError(f"unknown fn {fn!r}")
