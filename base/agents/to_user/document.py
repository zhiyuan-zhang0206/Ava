import hashlib
from datetime import UTC, datetime
from pathlib import Path

from base.agents.to_user.models import DOCUMENT_FILENAME, ToUserDocument
from base.agents.to_user.resources import (
    read_regular_file,
    resolve_reference,
    resource_descriptions,
)
from base.config import settings


def read_document(workspace: Path, *, references: list[str] | None = None) -> ToUserDocument:

    if workspace.is_symlink():
        raise ValueError("To user workspace must not be a symlink")
    path = resolve_reference(workspace, references) if references else workspace / DOCUMENT_FILENAME
    if path.suffix.lower() not in (".md", ".markdown"):
        raise ValueError("To user documents must be Markdown")
    empty = ToUserDocument(
        content=None,
        version=None,
        modified_at=None,
        legacy_html=not references and (workspace / "to_user.html").is_file(),
    )
    try:
        raw, info = read_regular_file(path, settings.to_user.max_document_bytes)
    except FileNotFoundError:
        if references:
            raise
        return empty
    content = raw.decode("utf-8")
    if not content.strip():
        return empty
    resources = resource_descriptions(path, content)
    revision = hashlib.sha256(raw)
    for href, resource in sorted(resources.items()):
        revision.update(f"\n{href}:{resource.version}".encode())
    return ToUserDocument(
        content=content,
        version=revision.hexdigest(),
        modified_at=datetime.fromtimestamp(info.st_mtime, UTC),
        resources=resources,
    )


def ensure_document(workspace: Path) -> None:
    path = workspace / DOCUMENT_FILENAME
    if not path.exists():
        path.touch()
