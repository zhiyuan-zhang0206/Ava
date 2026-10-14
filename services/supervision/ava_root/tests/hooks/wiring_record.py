"""Recording wiring participant for the daemon wiring tests, loaded with `--wiring`."""

import os
from pathlib import Path
from typing import Any


def build(context: Any) -> list[object]:
    del context
    markers = Path(os.environ["AVA_ROOT_WIRING_MARKERS"])
    markers.mkdir(parents=True, exist_ok=True)

    class Participant:
        def start(self) -> None:
            (markers / "started").write_text("yes", encoding="utf-8")

        def stop(self) -> None:
            (markers / "stopped").write_text("yes", encoding="utf-8")

    return [Participant()]
