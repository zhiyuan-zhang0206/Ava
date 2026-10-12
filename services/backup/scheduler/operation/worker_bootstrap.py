"""Entry of one operation worker process: `python -I -B worker_bootstrap.py ROOT MODULE ARGS...`.

Standard library only, so the code-root check runs before any first-party import. The
workers `MODULE` can name live in `services/backup/scheduler`. Each required name must
resolve inside ROOT, then MODULE runs as `__main__` with the remaining arguments.
"""

import importlib.util
import pathlib
import runpy
import sys


def main() -> None:
    root, module = pathlib.Path(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, str(root))
    for name in ("base", "services.backup.scheduler.operation", module):
        spec = importlib.util.find_spec(name)
        origin = None if spec is None else spec.origin
        if origin is None or not pathlib.Path(origin).resolve().is_relative_to(root):
            raise SystemExit(f"operation worker code root mismatch: {name} resolves to {origin}")
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
