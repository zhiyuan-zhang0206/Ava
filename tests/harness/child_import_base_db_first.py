"""Child process: importing `base.db` before `base.log` must not be a circular-import failure."""

import base.db  # pyright: ignore[reportUnusedImport]
import base.log  # noqa: F401  # pyright: ignore[reportUnusedImport]
