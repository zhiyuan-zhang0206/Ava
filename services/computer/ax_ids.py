"""Stable element ids for the accessibility tools.

The helper numbers each walk's nodes afresh ("raw ids") but stamps every node
with a path fingerprint (role + identifier/title + ordinal among same-keyed
siblings, chained down from the window). This module gives the same element the
same agent-visible id (`eN`) across walks by matching fingerprints, and keeps
the current raw id each stable id can be acted on through.

One table is live at a time, for one app process: the helper replaces its own
element table on every unscoped walk, so ids of any other app cannot be acted
on anyway. All of it is plain data with no I/O — alignment is tested without a
desktop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.computer.errors import ComputerUseError
from services.permissions_helper.client import AxNode

# Entries for elements that left the window are kept so a scrolled-away row
# regains its id when it returns; past this many, the vanished ones are pruned.
_PRUNE_ABOVE = 5000


@dataclass
class AxEntry:
    fp: str
    raw: int | None  # the helper's raw id in the latest walk, None when absent from it


@dataclass
class AxIdTable:
    """Stable ids for one app process (`pid`)."""

    app: str
    pid: int
    _next: int = field(default=1, init=False)
    _by_fp: dict[str, int] = field(default_factory=dict[str, int], init=False)
    _entries: dict[int, AxEntry] = field(default_factory=dict[int, AxEntry], init=False)

    def align(self, nodes: list[AxNode], *, scoped: bool) -> list[AxNode]:
        """The walk's nodes with `id` / `parent` rewritten to stable ids.

        An unscoped walk is the new truth: elements it did not see lose their raw
        id (acting on them is stale) but keep their stable id reserved. A scoped
        walk only refreshes the subtree it read."""
        if not scoped:
            for entry in self._entries.values():
                entry.raw = None
        stable_of_raw: dict[int, int] = {}
        used: set[int] = set()
        out: list[AxNode] = []
        for node in nodes:
            sid = self._by_fp.get(node["fp"])
            if sid is None or sid in used:
                # New element, or a fingerprint collision inside one walk: a
                # fresh id, never shared and never registered under the fp.
                sid = self._allocate()
                if node["fp"] not in self._by_fp:
                    self._by_fp[node["fp"]] = sid
            used.add(sid)
            self._entries[sid] = AxEntry(fp=node["fp"], raw=node["id"])
            stable_of_raw[node["id"]] = sid
            aligned: AxNode = {**node, "id": sid}
            if "parent" in node:
                aligned["parent"] = stable_of_raw[node["parent"]]
            out.append(aligned)
        self._prune()
        return out

    def entry(self, sid: int) -> AxEntry:
        try:
            return self._entries[sid]
        except KeyError:
            raise ComputerUseError(f"unknown element e{sid}: call ax_tree first") from None

    def raw_of(self, sid: int) -> int:
        """The raw id to act or scope through; the element must be in the latest walk."""
        raw = self.entry(sid).raw
        if raw is None:
            raise ComputerUseError(
                f"element e{sid} is not in the latest ax_tree of {self.app}: call ax_tree again"
            )
        return raw

    def _allocate(self) -> int:
        sid = self._next
        self._next += 1
        return sid

    def _prune(self) -> None:
        if len(self._entries) <= _PRUNE_ABOVE:
            return
        for sid in [sid for sid, e in self._entries.items() if e.raw is None]:
            fp = self._entries.pop(sid).fp
            if self._by_fp.get(fp) == sid:
                del self._by_fp[fp]


@dataclass
class AxSession:
    """The daemon's current table (one app process at a time)."""

    table: AxIdTable | None = None
    # The latest `include_ocr_gap` boxes, numbered px:N: physical-pixel centers
    # and (centre, label), plus the scale they were measured at. Replaced by
    # every ax_tree call; only `ax_act` press (a click) can use them.
    visual: dict[int, tuple[int, int, str]] = field(default_factory=dict[int, tuple[int, int, str]])
    visual_scale: float = 1.0

    def table_for(self, app: str, pid: int) -> AxIdTable:
        """The live table for this process; a different app or a restarted
        process (new pid) starts a fresh one, so old ids can never address it."""
        if self.table is None or self.table.pid != pid or self.table.app != app:
            self.table = AxIdTable(app=app, pid=pid)
        return self.table

    def current(self, app: str | None = None) -> AxIdTable:
        if self.table is None or (app is not None and self.table.app != app):
            raise ComputerUseError("no element ids for this app: call ax_tree first")
        return self.table
