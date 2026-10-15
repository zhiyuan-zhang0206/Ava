from __future__ import annotations

import asyncio
import importlib.abc
import sys
from datetime import UTC, datetime


class DenyConfiguration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "base.config" or fullname.startswith("base.config."):
            raise AssertionError("configuration requested: " + fullname)


def main() -> None:
    assert "base.config" not in sys.modules
    sys.meta_path.insert(0, DenyConfiguration())

    from base.clock import SYSTEM_TIMERS, Clock, ClockConfig, Timers

    moment = datetime(2026, 5, 6, 6, 32, 5, tzinfo=UTC)
    clock = Clock(
        ClockConfig("Asia/Shanghai", "Asia/Shanghai", False),
        timers=SYSTEM_TIMERS,
        now=lambda: moment,
    )
    timers: Timers = clock
    before = timers.monotonic()
    timers.sleep(0)
    asyncio.run(timers.asleep(0))
    assert timers.monotonic() >= before
    assert clock.now_timestamp() == "[2026-05-06 14:32:05]"
    assert "base.config" not in sys.modules
    try:
        Clock.from_settings()
    except AssertionError as exc:
        assert str(exc) == "configuration requested: base.config"
    else:
        raise AssertionError("the settings factory did not request configuration")
    assert "base.config" not in sys.modules


if __name__ == "__main__":
    main()
