"""Line previews with bounded archives protected by current context references.

Soft crop files have their own namespace: the legacy hard-overflow ring cannot
evict them. Native execs for one agent are serial. Only unreferenced soft files
are candidates for eviction, and an exhausted budget leaves the output inline.
"""

import re
import stat
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage

from shared.config.sandbox import SandboxSettings
from shared.lm.content import content_blocks
from shared.log import logger
from shared.message_kwargs import message_addl_kwargs, message_content

_CROP_NAME = re.compile(r"\bcrop_[0-9a-f]{32}\.txt\b")


def format_crop_preview(
    output: str, path: Path, *, after_lines: int, head_lines: int, tail_lines: int
) -> str | None:
    """Return a smaller preview or None; pure so offline tuning uses real markers."""
    if after_lines == 0:
        return None
    lines = output.splitlines(keepends=True)
    count = len(lines)
    if count <= after_lines or count <= head_lines + tail_lines:
        return None
    head = "".join(lines[:head_lines])
    tail = "".join(lines[-tail_lines:])
    marker = (
        f"[output cropped: {count:,} lines, {len(output):,} chars; "
        f"showing first {head_lines} + last {tail_lines} lines; "
        f"full output at {path} (read or grep it)]"
    )
    preview = head + ("" if head.endswith("\n") else "\n") + marker + "\n" + tail
    return preview if len(preview) < len(output) else None


def _references(messages: Sequence[BaseMessage]) -> set[str]:
    # UUID filenames are unique across agents. Matching the basename also keeps
    # references safe when a context note quotes a relative workspace path.
    referenced: set[str] = set()
    for message in messages:
        referenced.update(_CROP_NAME.findall(message.text))
        content = message_content(message)
        if isinstance(content, list):
            for block in content_blocks(content):
                if isinstance(block, dict) and block.get("type") in {
                    "thinking",
                    "reasoning",
                    "tool_use",
                }:
                    referenced.update(_CROP_NAME.findall(str(block)))
        if isinstance(message, AIMessage):
            # execute_code arguments are model-visible context too, even when
            # the accompanying assistant text is empty.
            referenced.update(_CROP_NAME.findall(str(message.tool_calls)))
            referenced.update(_CROP_NAME.findall(str(message.invalid_tool_calls)))
            metadata = message_addl_kwargs(message)
            referenced.update(_CROP_NAME.findall(str(metadata.get("reasoning_content", ""))))
    return referenced


def _make_room(directory: Path, size: int, budget: int, referenced: set[str]) -> bool:
    if size > budget:
        return False
    existing: list[tuple[Path, int, float]] = []
    for path in directory.glob("crop_*.txt"):
        metadata = path.lstat()
        if _CROP_NAME.fullmatch(path.name) and stat.S_ISREG(metadata.st_mode):
            existing.append((path, metadata.st_size, metadata.st_mtime))
    protected_bytes = sum(size for path, size, _ in existing if path.name in referenced)
    if protected_bytes + size > budget:
        return False
    total = sum(size for _, size, _ in existing) + size
    for path, old_size, _ in sorted(existing, key=lambda entry: entry[2]):
        if total <= budget:
            break
        if path.name not in referenced:
            path.unlink()
            total -= old_size
    return True


def crop_output(
    output: str,
    directory: Path,
    config: SandboxSettings,
    *,
    referenced_messages: Sequence[BaseMessage],
    max_chars: int,
) -> str | None:
    """Archive before returning a preview; capacity/storage failures keep the body."""
    path = directory / f"crop_{uuid4().hex}.txt"
    preview = format_crop_preview(
        output,
        path,
        after_lines=config.exec_output_crop_after_lines,
        head_lines=config.exec_output_crop_head_lines,
        tail_lines=config.exec_output_crop_tail_lines,
    )
    if preview is None or len(preview) > max_chars:
        return None
    encoded = output.encode("utf-8")
    created = False
    try:
        if not _make_room(
            directory,
            len(encoded),
            config.exec_output_crop_archive_max_bytes,
            _references(referenced_messages) | set(_CROP_NAME.findall(output)),
        ):
            logger.info("Soft output crop skipped: referenced archives or output fill byte budget")
            return None
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as archive:
            created = True
            path.chmod(0o600)
            archive.write(encoded)
    except OSError as exc:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning("Soft output archive cleanup failed ({error})", error=cleanup_error)
        logger.warning("Soft output crop skipped: archive unavailable ({error})", error=exc)
        return None
    return preview
