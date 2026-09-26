"""Claude Code first-run presets — an unattended spawn must never park on a dialog.

Split out of ``spawn_claude.py`` (2026-09-24, task #4612) so the launcher
stays under the 800-line hard ceiling: it imports ``_preset_claude_first_run``
and runs it before any session starts.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path


def _preset_json_file(path: Path, updates: dict[str, object], *, label: str) -> None:
    """Back up, merge ``updates`` into a JSON file, atomically; see _preset_claude_first_run.

    Existing keys are preserved verbatim; a satisfied update makes the call a
    no-op; an unparsable file is backed up and raises (fail fast - overlaying
    it would destroy the owner's config).
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        data = None
    except json.JSONDecodeError as exc:
        _backup_before_write(path)
        raise RuntimeError(
            f"{label} is not valid JSON ({exc}); left as-is with a backup taken. "
            "Fix or remove it, then relaunch."
        ) from exc
    if data is not None and not isinstance(data, dict):
        _backup_before_write(path)
        raise RuntimeError(f"{label} is not a JSON object; left as-is with a backup taken.")
    if data is not None and all(_preset_satisfied(data.get(k), v) for k, v in updates.items()):
        print(f"(already preset: {label})")
        return
    if data is None:
        data = {}
    if path.exists():
        _backup_before_write(path)
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, data)
    print(f"+ preset: {label}")


def _preset_satisfied(current: object, desired: object) -> bool:
    """True when `current` already carries the desired preset value."""
    if isinstance(desired, bool):
        return current is desired
    if isinstance(desired, int):
        return (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and current >= desired
        )
    return current == desired


def _backup_before_write(path: Path) -> Path:
    """Copy `path` to a uniquely named sibling backup; same-second callers never collide."""
    fd, raw_backup = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-"
    )
    os.close(fd)
    backup = Path(raw_backup)
    backup.write_bytes(path.read_bytes())
    return backup


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    """Publish `data` at `path` through a mkstemp-unique tmp file + atomic replace.

    A unique tmp name means two concurrent presets in the same second cannot
    collide; an existing file keeps its permission bits.
    """
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(raw_tmp)
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _preset_claude_first_run(home: Path | None = None) -> None:
    """Preset Claude Code's first-run dialogs so an unattended spawn never parks on one.

    Two dialogs break a non-interactive spawn on a fresh HOME: the
    bypass-permissions confirmation (>=2.1.274 defaults to No/exit, so a blind
    Enter kills the session) and the fullscreen upsell. Both answers persist in
    files, so presetting them is enough:

    - ``~/.claude/settings.json``: ``skipDangerousModePermissionPrompt: true``
      ("whether the user has accepted the bypass permissions mode dialog").
    - ``~/.claude.json``: ``fullscreenUpsellSeenCount: 3`` - 2.1.278 shows the
      upsell while this is below its threshold of 3 (bundle: ``<x8e``, x8e=3).

    Each file is backed up before its first change, a second call is a no-op,
    and an unparsable file is backed up and raises instead of being
    overwritten. One line per file: ``+ preset`` / ``(already preset``.
    """
    home = Path.home() if home is None else home
    _preset_json_file(
        home / ".claude" / "settings.json",
        {"skipDangerousModePermissionPrompt": True},
        label="~/.claude/settings.json",
    )
    _preset_json_file(
        home / ".claude.json",
        {"fullscreenUpsellSeenCount": 3},
        label="~/.claude.json",
    )
