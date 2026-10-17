import json
from collections import Counter
from pathlib import Path
from typing import TypedDict

from scripts.content_lint.code_comments.check import Repository
from scripts.content_lint.code_comments.model import NoteKind


class Entry(TypedDict):
    path: str
    line: int
    owner: str
    text: str
    links: list[str]


def entries(repo: Repository) -> list[Entry]:
    rows: list[Entry] = []
    for path in sorted(repo.sources):
        for note in repo.sources[path].notes:
            if note.kind != NoteKind.NOTE:
                continue
            rows.append(
                {
                    "path": path,
                    "line": note.line,
                    "owner": note.owner,
                    "text": note.text,
                    "links": list(note.links),
                }
            )
    return rows


def jsonl(rows: list[Entry]) -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"


def percentiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": ordered[len(ordered) // 2],
        "p90": ordered[int(len(ordered) * 0.9)],
        "max": ordered[-1],
    }


def _repeated_text(rows: list[Entry]) -> list[str]:
    texts = Counter(row["text"] for row in rows if row["text"].strip())
    duplicates = [(text, count) for text, count in texts.most_common(10) if count > 1]
    if not duplicates:
        return ["None."]
    return [
        f"- {count}x {text if len(text) <= 120 else text[:117] + '...'}"
        for text, count in duplicates
    ]


def _note_rows(rows: list[Entry]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.append(f"- `{row['path']}:{row['line']}` ({row['owner']}) {row['text']}")
        lines.extend(f"  - link: `{link}`" for link in row["links"])
    return lines


def markdown(rows: list[Entry]) -> str:
    written = [row for row in rows if row["text"].strip()]
    lines = [
        "# Source note corpus",
        "",
        f"Notes: {len(rows)} ({len(written)} with text, {len(rows) - len(written)} link-only)",
        f"Text length: {percentiles([len(row['text']) for row in written])}",
        f"Notes with links: {sum(1 for row in rows if row['links'])}",
        f"Links: {sum(len(row['links']) for row in rows)}",
        "",
        "## Repeated text",
        "",
        *_repeated_text(rows),
        "",
        "## Notes",
        "",
        *_note_rows(rows),
    ]
    return "\n".join(lines) + "\n"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
