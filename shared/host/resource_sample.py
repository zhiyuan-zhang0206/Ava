"""One live CPU / memory / disk / battery reading for this machine — no history.

The status surfaces used to carry a 300-sample ring buffer per machine (the
retired `shared.resource_monitor`). Since issue #46 the host metrics live in
Prometheus, scraped by the per-machine OTel Collector sidecar's `host_metrics`
receiver, and Prometheus is the ONE time-series store: a second retained
history would be a second answer to "what was the CPU on machine X" that drifts
from the first.

What survives here is the degraded answer — the reading `ava status` and the
status page must still show on a deployment where the LGTM backend is down or
was never deployed (observability is optional; `ava lgtm off` is a supported
state). It is a live one-shot sample and nothing is kept between calls, so
there is no cadence to keep, nothing to lose on restart, and no drift.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from shared.platform import primary_disk_path

# psutil's non-blocking cpu_percent reports the average since the PREVIOUS call
# in the same process, which is meaningless for a stateless one-shot: the first
# call returns 0.0 and later ones cover whatever interval the last poller left
# behind. A short blocking measurement is self-contained; the callers already
# run this off the event loop.
_CPU_INTERVAL_S = 0.1

# task #3696 exception inventory: a protective bound on one best-effort local
# read (`pmset -g batt`) so a hung command cannot stall the status probe that
# consumes this sample. The probe's own 8s budget
# (AVA_STATUS_PROBE_TIMEOUT_SECONDS) sets the ceiling; not an operator knob.
_BATTERY_READ_TIMEOUT_S = 2.0


class _BatteryFields(TypedDict, total=False):
    """The battery keys `_parse_battery` may emit; an absent key means unknown."""

    battery_percent: int
    battery_power: Literal["ac", "battery"]
    battery_charging: bool
    battery_remaining_min: int


class ResourceSample(BaseModel):
    """This machine's CPU / memory / disk / battery at the moment it was read."""

    model_config = ConfigDict(frozen=True)

    ts: float  # unix timestamp
    cpu_pct: float  # CPU usage percent (0-100)
    mem_used_gb: float
    mem_total_gb: float
    mem_pct: float  # memory usage percent
    disk_used_gb: float
    disk_total_gb: float
    disk_pct: float  # disk usage percent
    # Battery telemetry (macOS laptops, task #3743). None when the machine has
    # no battery or the reading failed — never a fabricated default.
    battery_percent: int | None = None
    battery_power: Literal["ac", "battery"] | None = None
    battery_charging: bool | None = None
    battery_remaining_min: int | None = None


def _parse_battery(out: str) -> _BatteryFields:
    """Parse `pmset -g batt` text into the battery fields; {} when no battery.

    Sample input (a battery-powered macOS host on AC):
        Now drawing from 'AC Power'
         -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true

    Desktops print only the first line — no battery, so every field stays None.
    "no estimate" (no time-to-empty yet) omits battery_remaining_min alone; an
    unknown state string omits battery_charging rather than guessing.
    """
    m = re.search(r"(\d+)%;\s*([^;\n]+)", out)
    if m is None:
        return {}
    fields: _BatteryFields = {"battery_percent": int(m.group(1))}
    state = m.group(2).strip().lower()
    if state in ("charging", "finishing charge"):
        fields["battery_charging"] = True
    elif state in ("discharging", "charged", "ac attached", "not charging"):
        fields["battery_charging"] = False
    p = re.search(r"drawing from '([^']+)'", out)
    if p:
        if p.group(1) == "AC Power":
            fields["battery_power"] = "ac"
        elif p.group(1) == "Battery Power":
            fields["battery_power"] = "battery"
    r = re.search(r"(\d+):(\d{2})\s*remaining", out)
    if r:
        fields["battery_remaining_min"] = int(r.group(1)) * 60 + int(r.group(2))
    return fields


def _battery_sample() -> _BatteryFields:
    """One best-effort battery reading; {} off macOS / on failure / no battery.

    Swallows failures by design: the sample degrades to None fields, it does
    not fail the probe (unlike the psutil reads, which propagate).
    """
    if sys.platform != "darwin":
        return {}
    try:
        out = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_BATTERY_READ_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return _parse_battery(out)


def resource_sample() -> ResourceSample:
    """Read this machine's CPU / memory / primary-disk usage — and, on a macOS
    battery host, its battery state — right now.

    Raises whatever psutil raises (including ImportError when it is absent) —
    the status callers decide whether a missing reading degrades the row or
    fails the probe.
    """
    import psutil

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(primary_disk_path())

    # psutil's `percent` is the canonical fullness/pressure figure, but it is
    # measured against a *different* denominator than the naive `.total`, so
    # `used / total` disagreed with the shown percent (disk read 69% next to
    # an 84% bar, memory 34% next to 80%). Derive the displayed used/total
    # pair from that same denominator so the panel is self-consistent —
    # shown_used / shown_total == shown_percent:
    #   * disk.percent == used / (used + free)  (== `df` Capacity; the
    #     root-reserved blocks that pad `disk.total` are excluded), so the
    #     shown total is used + free, the space actually available to us.
    #   * mem.percent == (total - available) / total, where `available`
    #     counts reclaimable cache; `mem.used` alone undercounts on macOS, so
    #     the shown used is total - available (real pressure), total is RAM.
    disk_total = disk.used + disk.free
    mem_used = mem.total - mem.available

    return ResourceSample(
        ts=time.time(),
        cpu_pct=psutil.cpu_percent(interval=_CPU_INTERVAL_S),
        mem_used_gb=round(mem_used / (1024**3), 2),
        mem_total_gb=round(mem.total / (1024**3), 2),
        mem_pct=mem.percent,
        disk_used_gb=round(disk.used / (1024**3), 2),
        disk_total_gb=round(disk_total / (1024**3), 2),
        disk_pct=disk.percent,
        **_battery_sample(),
    )
