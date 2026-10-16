import os
from pathlib import Path

import pytest

from base.agents.to_user import (
    MAX_DOCUMENT_BYTES,
    MAX_RESOURCE_BYTES,
    ToUserReadRequest,
    ensure_document,
    read_document,
    read_resource,
)


def test_missing_empty_and_legacy_html(tmp_path: Path) -> None:
    assert read_document(tmp_path).content is None
    (tmp_path / "to_user.html").write_text("<p>Previous context</p>")
    assert read_document(tmp_path).legacy_html
    (tmp_path / "to_user.md").write_text("  ")
    assert read_document(tmp_path).content is None
    (tmp_path / "to_user.md").write_text("# Ready")
    assert read_document(tmp_path).content == "# Ready"
    assert not read_document(tmp_path).legacy_html


def test_atomic_replacement_changes_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "to_user.md"
    target.write_text("Working")
    first = read_document(tmp_path)
    replacement = tmp_path / ".next"
    replacement.write_text("Ready")
    replacement.replace(target)
    second = read_document(tmp_path)
    assert second.content == "Ready"
    assert second.version != first.version
    assert second.modified_at is not None


def test_symlinks_devices_invalid_encoding_and_oversize_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "to_user.md"
    secret = tmp_path / "private"
    secret.write_text("not a document")
    target.symlink_to(secret)
    with pytest.raises(OSError):
        read_document(tmp_path)
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular file"):
        read_document(tmp_path)
    target.unlink()
    target.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        read_document(tmp_path)
    target.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ValueError, match="byte limit"):
        read_document(tmp_path)


def test_relative_absolute_file_uri_and_reference_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    asset = tmp_path / "a b.png"
    asset.write_bytes(b"image")
    source = "\n".join(
        [
            "![relative](../a%20b.png)",
            f"[absolute](<{asset}>)",
            f"[uri]({asset.as_uri()})",
            "[reference][asset]",
            "[asset]: ../a%20b.png",
            "[external](https://example.com/report.pdf)",
            "[anchor](#section)",
            "`[code](secret)`",
            "```md\n[code](other-secret)\n```",
            '<img src="html-secret">',
        ]
    )
    (workspace / "to_user.md").write_text(source)
    document = read_document(workspace)
    assert len(document.resources) == 4
    for href, resource in document.resources.items():
        if href == "html-secret":
            assert resource.name == "html-secret"
            continue
        assert resource.name == "a b.png"
        assert read_resource(workspace, [resource.id]) == b"image"
    with pytest.raises(FileNotFoundError):
        read_resource(workspace, ["0" * 64])


def test_nested_markdown_uses_its_own_directory_and_rechecks_chain(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    (tmp_path / "to_user.md").write_text("[report](report/index.md)")
    (report / "index.md").write_text("![plot](plot.png)")
    (report / "plot.png").write_bytes(b"plot")
    first = next(iter(read_document(tmp_path).resources.values()))
    child = read_document(tmp_path, references=[first.id])
    second = next(iter(child.resources.values()))
    assert read_resource(tmp_path, [first.id, second.id]) == b"plot"
    (report / "index.md").write_text("Link removed")
    with pytest.raises(FileNotFoundError):
        read_resource(tmp_path, [first.id, second.id])


def test_linked_file_revision_changes_without_document_edit(tmp_path: Path) -> None:
    (tmp_path / "to_user.md").write_text("![plot](plot.png)")
    (tmp_path / "plot.png").write_bytes(b"before")
    before = read_document(tmp_path)
    (tmp_path / "plot.png").write_bytes(b"after")
    after = read_document(tmp_path)
    assert before.version != after.version
    assert before.resources["plot.png"].version != after.resources["plot.png"].version


def test_resource_limits_and_removed_file(tmp_path: Path) -> None:
    (tmp_path / "to_user.md").write_text("[file](data.bin)")
    target = tmp_path / "data.bin"
    with target.open("wb") as stream:
        stream.truncate(MAX_RESOURCE_BYTES + 1)
    resource = next(iter(read_document(tmp_path).resources.values()))
    with pytest.raises(ValueError, match="byte limit"):
        read_resource(tmp_path, [resource.id])
    target.unlink()
    with pytest.raises(FileNotFoundError):
        read_resource(tmp_path, [resource.id])
    with pytest.raises(ValueError):
        ToUserReadRequest(agent_id=1, references=["/etc/passwd"])
    with pytest.raises(ValueError):
        ToUserReadRequest(agent_id=1, references=["0" * 64] * 9)


def test_home_reference_and_symlink_target(tmp_path: Path) -> None:

    name = os.path.relpath(tmp_path / "file.txt", Path.home())
    (tmp_path / "file.txt").write_text("shared")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "file.txt")
    (tmp_path / "to_user.md").write_text(f"[home](~/{name})\n[alias](alias.txt)")
    document = read_document(tmp_path)
    assert len(document.resources) == 2
    for resource in document.resources.values():
        assert read_resource(tmp_path, [resource.id]) == b"shared"


def test_raw_html_images_and_links_are_resources(tmp_path: Path) -> None:
    (tmp_path / "to_user.md").write_text(
        '<img src="chart.png" alt="chart">\n\n'
        'Inline <a href="notes.md">notes</a> link\n\n'
        '<img src="a&amp;b.png">\n'
    )
    (tmp_path / "chart.png").write_bytes(b"png")
    (tmp_path / "notes.md").write_text("# Notes")
    (tmp_path / "a&b.png").write_bytes(b"amp")
    document = read_document(tmp_path)
    assert set(document.resources) == {"chart.png", "notes.md", "a&b.png"}
    assert document.resources["notes.md"].markdown


def test_footnote_links_are_resources_but_raw_html_blocks_are_not(tmp_path: Path) -> None:
    (tmp_path / "to_user.md").write_text(
        "See note[^1]\n\n[^1]: [report](report.pdf)\n\n<div>\n[hidden](private.txt)\n</div>\n"
    )
    (tmp_path / "report.pdf").write_bytes(b"pdf")
    document = read_document(tmp_path)
    assert set(document.resources) == {"report.pdf"}
    resource = document.resources["report.pdf"]
    assert read_resource(tmp_path, [resource.id]) == b"pdf"


def test_ensure_document_seeds_only_a_missing_panel(tmp_path: Path) -> None:
    ensure_document(tmp_path)
    panel = tmp_path / "to_user.md"
    assert panel.read_text() == ""
    panel.write_text("# Keep me")
    marker = panel.stat().st_mtime_ns
    ensure_document(tmp_path)
    assert panel.read_text() == "# Keep me"
    assert panel.stat().st_mtime_ns == marker
