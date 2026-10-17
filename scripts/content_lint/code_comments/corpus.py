import json
import statistics
from collections import Counter
from pathlib import Path

from scripts.content_lint.code_comments.check import Repository
from scripts.content_lint.code_comments.model import NoteKind


def entries(repo: Repository) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
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


def jsonl(rows: list[dict[str, object]]) -> str:
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


def markdown(rows: list[dict[str, object]]) -> str:
    lengths = [len(str(row["text"])) for row in rows]
    texts = Counter(str(row["text"]) for row in rows if str(row["text"]).strip())
    duplicates = [(text, count) for text, count in texts.most_common(10) if count > 1]
    lines = [
        "# Source note corpus",
        "",
        f"Notes: {len(rows)}",
        f"Length: {percentiles(lengths)}",
        f"Notes with links: {sum(1 for row in rows if row['links'])}",
        f"Links: {sum(len(row['links']) for row in rows)}",
        "",
        "## Repeated text",
        "",
    ]
    if duplicates:
        for text, count in duplicates:
            summary = text if len(text) <= 120 else text[:117] + "..."
            lines.append(f"- {count}x {summary}")
    else:
        lines.append("None.")
    lines.extend(["", "## Notes", ""])
    for row in rows:
        lines.append(f"- `{row['path']}:{row['line']}` ({row['owner']}) {row['text']}")
        for link in row["links"]:
            lines.append(f"  - link: `{link}`")
    return "\n".join(lines) + "\n"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
