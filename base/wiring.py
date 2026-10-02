"""Markers for composition-root wiring.

`root_bundle` marks a frozen dataclass that a composition root builds to hold the pieces it
wires (handles, slices) in one local value. The bundle is the root's own variable: it is
unpacked there and never handed to library code, because a function that receives the whole
bundle can reach any member, which is a service locator again. The `bundle-leak` rule in
scripts/structure/ambient_state fails any annotation of a bundle outside its defining module.
"""

from __future__ import annotations


def root_bundle[T: type](cls: T) -> T:
    """Mark `cls` as a root-local bundle (no runtime effect; the lint reads the decorator)."""
    return cls
