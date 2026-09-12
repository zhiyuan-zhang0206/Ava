"""Unit manifests and the tree registry (K2 contract).

The root supervisor owns a tree of long-lived processes; the tree's shape is a
direct result of the declared unit manifests — there are no implicit members.

This module is deliberately data-only: it parses manifest files, validates
every field fail-fast (the K2 field set is closed, so an unknown field is an
error rather than a silently ignored field), and lays the tree out. Nothing
here spawns or stops anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

# The implicit tree root. Units attach here by default; it is never a unit
# itself and cannot be declared as one.
ROOT_ID = "root"

# Unit ids are lowercase slugs: stable, filesystem-safe, log-safe.
_UNIT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The closed K2 field sets. A new field must go through design first — the
# validators reject anything else, so field drift fails loudly instead of
# being carried around silently.
_REQUIRED_FIELDS = frozenset({"id", "exec", "restart"})
_MANIFEST_FIELDS = _REQUIRED_FIELDS | {"attach"}
_FILE_FIELDS = frozenset({"units"})


class ManifestError(ValueError):
    """A manifest file or a unit manifest is malformed (fail-fast; no fallback)."""


class UnknownUnitError(LookupError):
    """The requested unit id is not part of the registry."""


class RestartPolicy(StrEnum):
    """When the supervisor restarts a unit after its process exits."""

    ALWAYS = "always"
    ON_FAILURE = "on-failure"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class UnitManifest:
    """One declared unit (K2): the minimal, closed field set."""

    id: str
    exec: tuple[str, ...]
    restart: RestartPolicy
    attach: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, origin: str) -> UnitManifest:
        """Validate one manifest mapping; every deviation raises ManifestError."""
        unknown = set(raw) - _MANIFEST_FIELDS
        if unknown:
            raise ManifestError(
                f"{origin}: unknown manifest field(s) {sorted(unknown)}; the K2 field set is "
                "closed — extend it through design first"
            )
        missing = _REQUIRED_FIELDS - set(raw)
        if missing:
            raise ManifestError(f"{origin}: missing required field(s) {sorted(missing)}")

        unit_id = _require_str(raw, "id", origin)
        if unit_id == ROOT_ID:
            raise ManifestError(f"{origin}: id {ROOT_ID!r} is reserved for the tree root")
        if not _UNIT_ID_RE.match(unit_id):
            raise ManifestError(
                f"{origin}: id {unit_id!r} must be a lowercase slug "
                "(start with [a-z0-9], then [a-z0-9._-])"
            )

        exec_raw = raw["exec"]
        if not isinstance(exec_raw, list) or not exec_raw:
            raise ManifestError(f"{origin}: exec must be a non-empty argv list")
        argv: list[str] = []
        for index, part in enumerate(cast("list[object]", exec_raw)):
            if not isinstance(part, str) or not part:
                raise ManifestError(f"{origin}: exec[{index}] must be a non-empty string")
            argv.append(part)

        restart_raw = _require_str(raw, "restart", origin)
        try:
            restart = RestartPolicy(restart_raw)
        except ValueError as exc:
            choices = [policy.value for policy in RestartPolicy]
            raise ManifestError(
                f"{origin}: restart {restart_raw!r} is not one of {choices}"
            ) from exc

        attach = raw.get("attach", ROOT_ID)
        if not isinstance(attach, str):
            raise ManifestError(f"{origin}: attach must be a string")
        if attach != ROOT_ID and not _UNIT_ID_RE.match(attach):
            raise ManifestError(f"{origin}: attach {attach!r} is not a valid unit id")

        return cls(id=unit_id, exec=tuple(argv), restart=restart, attach=attach)


def _require_str(raw: Mapping[str, object], field: str, origin: str) -> str:
    value = raw[field]
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{origin}: {field} must be a non-empty string")
    return value


class UnitRegistry:
    """The tree shape, derived directly from a closed set of manifests.

    Construction validates the whole forest once: duplicate ids, attachments to
    unknown units, and attach cycles all raise ManifestError.
    """

    def __init__(self, manifests: Sequence[UnitManifest]) -> None:
        by_id: dict[str, UnitManifest] = {}
        for manifest in manifests:
            if manifest.id in by_id:
                raise ManifestError(f"duplicate unit id {manifest.id!r}")
            by_id[manifest.id] = manifest
        children: dict[str, list[str]] = {}
        for manifest in manifests:
            if manifest.attach != ROOT_ID and manifest.attach not in by_id:
                raise ManifestError(
                    f"unit {manifest.id!r} attaches to unknown unit {manifest.attach!r}"
                )
            children.setdefault(manifest.attach, []).append(manifest.id)
        self._validate_acyclic(by_id)
        self._manifests = by_id
        self._children = {parent: tuple(kids) for parent, kids in children.items()}

    @staticmethod
    def _validate_acyclic(by_id: Mapping[str, UnitManifest]) -> None:
        """Walk every unit's attach chain; a revisited link is a cycle."""
        for manifest in by_id.values():
            seen: set[str] = set()
            cursor = manifest.id
            while cursor != ROOT_ID:
                if cursor in seen:
                    raise ManifestError(f"attach cycle reached from unit {manifest.id!r}")
                seen.add(cursor)
                cursor = by_id[cursor].attach

    def get(self, unit_id: str) -> UnitManifest:
        """Return the manifest for `unit_id`; UnknownUnitError when absent."""
        try:
            return self._manifests[unit_id]
        except KeyError:
            raise UnknownUnitError(f"unknown unit {unit_id!r}") from None

    @property
    def units(self) -> tuple[UnitManifest, ...]:
        """Every unit manifest, in declaration order."""
        return tuple(self._manifests.values())

    def children_of(self, unit_id: str) -> tuple[str, ...]:
        """Direct attach children of `unit_id` (ROOT_ID allowed), declaration order."""
        if unit_id != ROOT_ID:
            self.get(unit_id)
        return self._children.get(unit_id, ())

    def subtree(self, unit_id: str) -> tuple[str, ...]:
        """`unit_id` and all attach descendants, parents before children."""
        self.get(unit_id)
        out: list[str] = []
        stack = [unit_id]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(reversed(self._children.get(current, ())))
        return tuple(out)

    def stop_order(self, unit_id: str) -> tuple[str, ...]:
        """`unit_id`'s subtree, children before parents (reverse of startup)."""
        return tuple(reversed(self.subtree(unit_id)))

    def all_stop_order(self) -> tuple[str, ...]:
        """Every unit, children before parents; the whole-tree stop order."""
        out: list[str] = []
        for top in self.children_of(ROOT_ID):
            out.extend(self.stop_order(top))
        return tuple(out)


def load_manifests(path: Path) -> UnitRegistry:
    """Read a manifest file into a validated registry (fail-fast)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest file {path}: {exc}") from exc
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise ManifestError(f"{path}: top level must be an object with a 'units' list")
    document = cast("dict[str, object]", root)
    unknown = set(document) - _FILE_FIELDS
    if unknown:
        raise ManifestError(f"{path}: unknown top-level field(s) {sorted(unknown)}")
    units_raw = document.get("units")
    if not isinstance(units_raw, list):
        raise ManifestError(f"{path}: 'units' must be a list")
    manifests: list[UnitManifest] = []
    for index, item in enumerate(cast("list[object]", units_raw)):
        if not isinstance(item, dict):
            raise ManifestError(f"{path}: units[{index}] must be an object")
        manifests.append(
            UnitManifest.from_mapping(
                cast("dict[str, object]", item), origin=f"{path}: units[{index}]"
            )
        )
    return UnitRegistry(manifests)
