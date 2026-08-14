import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ava_home

mp = str(ava_home() / "memory")
RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


def frontmatter(rel):
    with open(os.path.join(mp, rel), encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def collect():
    files = []
    for dirpath, _dirnames, filenames in os.walk(mp):
        if ".git" in dirpath or ".githooks" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md") or fn in RESERVED:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), mp)
            files.append(rel)
    return files


def write_index(dir_rel):
    '""OKF spec §8: index.md enumerates the directory\'s contents (no frontmatter).""'
    dpath = os.path.join(mp, dir_rel) if dir_rel else mp
    entries = sorted(os.listdir(dpath))
    dirs = [e for e in entries if os.path.isdir(os.path.join(dpath, e)) and not e.startswith(".")]
    mds = [e for e in entries if e.endswith(".md") and e not in RESERVED]

    title = "(root)" if not dir_rel else dir_rel + "/"
    lines = [f"# {title}", ""]
    lines.append("## Subdirectories")
    lines.append("")
    for d in dirs:
        n = len(
            [f for f in collect() if f.startswith((dir_rel + "/" if dir_rel else "") + d + "/")]
        )
        lines.append(f"* [{d}/]({d}/) - {n} notes")
    if not dirs:
        lines.append("*(none)*")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for md in mds:
        rel = (dir_rel + "/" if dir_rel else "") + md
        fm = frontmatter(rel)
        t = str(fm.get("title") or md[:-3])
        d = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"* [{t}]({md}) - {d}")
    if not mds:
        lines.append("*(none)*")
    lines.append("")

    target = os.path.join(dpath, "index.md") if dir_rel else os.path.join(mp, "index.md")
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return target


files = collect()
dirs_with_notes = set()
for f in files:
    d = os.path.dirname(f)
    while d:
        dirs_with_notes.add(d)
        d = os.path.dirname(d)
    dirs_with_notes.add("")  # root

written = []
for d in sorted(dirs_with_notes, key=lambda x: (x.count("/"), x)):
    written.append(write_index(d))
print("index.md written:", len(written))
