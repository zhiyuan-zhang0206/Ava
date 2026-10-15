import hashlib
import os
import stat
from pathlib import Path
from typing import override
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

from base.agents.to_user.models import (
    DOCUMENT_FILENAME,
    MAX_DOCUMENT_BYTES,
    MAX_REFERENCES,
    MAX_RESOURCE_BYTES,
    ToUserResource,
)


class DocumentMarkdown(MarkdownIt):
    @override
    def validateLink(self, url: str) -> bool:
        return urlsplit(url).scheme == "file" or super().validateLink(url)


def local_target(document: Path, href: str) -> Path | None:

    url = urlsplit(href)
    if url.scheme not in ("", "file") or url.netloc not in ("", "localhost"):
        return None
    if not url.path or href.startswith("//"):
        return None
    value = unquote(url.path)
    if "\x00" in value:
        raise ValueError("A document reference contains a null byte")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else document.parent / path).resolve()


def referenced_files(document: Path, content: str) -> dict[str, Path]:

    parser = DocumentMarkdown("commonmark", {"html": True}).enable("table").use(footnote_plugin)
    pending = list(parser.parse(content))
    references: dict[str, Path] = {}
    while pending:
        token = pending.pop()
        pending.extend(token.children or [])
        href = token.attrGet("href") if token.type == "link_open" else token.attrGet("src")
        if isinstance(href, str):
            path = local_target(document, href)
            if path is not None:
                references[href] = path
                if len(references) > MAX_REFERENCES:
                    raise ValueError("To user exceeds the 256 local reference limit")
    return references


def reference_id(href: str) -> str:
    return hashlib.sha256(href.encode()).hexdigest()


def resource_descriptions(document: Path, content: str) -> dict[str, ToUserResource]:
    result: dict[str, ToUserResource] = {}
    for href, path in referenced_files(document, content).items():
        try:
            info = path.stat()
            stamp = (
                f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
            )
            version = hashlib.sha256(stamp.encode()).hexdigest()
        except OSError:
            version = None
        result[href] = ToUserResource(
            id=reference_id(href),
            name=path.name,
            markdown=path.suffix.lower() in (".md", ".markdown"),
            version=version,
        )
    return result


def read_regular_file(path: Path, limit: int) -> tuple[bytes, os.stat_result]:

    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("To user references must be regular files")
        if info.st_size > limit:
            raise ValueError(f"To user file exceeds the {limit} byte limit")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"To user file exceeds the {limit} byte limit")
    return raw, info


def resolve_reference(workspace: Path, references: list[str]) -> Path:

    if workspace.is_symlink():
        raise ValueError("To user workspace must not be a symlink")
    if not references or len(references) > 8:
        raise ValueError("A resource requires between one and eight reference IDs")
    path = workspace / DOCUMENT_FILENAME
    for resource_id in references:
        if path.suffix.lower() not in (".md", ".markdown"):
            raise ValueError("Only Markdown documents can link further resources")
        raw, _ = read_regular_file(path, MAX_DOCUMENT_BYTES)
        targets = referenced_files(path, raw.decode("utf-8"))
        matches = [target for href, target in targets.items() if reference_id(href) == resource_id]
        if not matches:
            raise FileNotFoundError("The document no longer references this resource")
        path = matches[0]
    return path


def read_resource(workspace: Path, references: list[str]) -> bytes:
    path = resolve_reference(workspace, references)
    return read_regular_file(path, MAX_RESOURCE_BYTES)[0]
