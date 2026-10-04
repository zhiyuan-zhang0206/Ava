import os
import re

import yaml

from ava_builtins.plugins.ava_memory.pool_ops import pool_dir


def _pool() -> str:
    return str(pool_dir())


RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


def frontmatter(rel):
    with open(os.path.join(_pool(), rel), encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:  # malformed frontmatter: the note is indexed without one
        return {}


def collect():
    files = []
    for dirpath, _dirnames, filenames in os.walk(_pool()):
        if ".git" in dirpath or ".githooks" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".md") or fn in RESERVED:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), _pool())
            files.append(rel)
    return files


def _subdir_lines(dir_rel, dirs):
    lines = []
    for d in dirs:
        n = len(
            [f for f in collect() if f.startswith((dir_rel + "/" if dir_rel else "") + d + "/")]
        )
        lines.append(f"* [{d}/]({d}/) - {n} notes")
    return lines or ["*(none)*"]


def _note_lines(dir_rel, mds):
    lines = []
    for md in mds:
        rel = (dir_rel + "/" if dir_rel else "") + md
        fm = frontmatter(rel)
        t = str(fm.get("title") or md[:-3])
        d = str(fm.get("description") or "").replace("\n", " ")
        lines.append(f"* [{t}]({md}) - {d}")
    return lines or ["*(none)*"]


def write_index(dir_rel):
    '""OKF spec §8: index.md enumerates the directory\'s contents (no frontmatter).""'
    dpath = os.path.join(_pool(), dir_rel) if dir_rel else _pool()
    entries = sorted(os.listdir(dpath))
    dirs = [e for e in entries if os.path.isdir(os.path.join(dpath, e)) and not e.startswith(".")]
    mds = [e for e in entries if e.endswith(".md") and e not in RESERVED]

    title = "(root)" if not dir_rel else dir_rel + "/"
    lines = [f"# {title}", "", "## Subdirectories", ""]
    lines.extend(_subdir_lines(dir_rel, dirs))
    lines.extend(["", "## Notes", ""])
    lines.extend(_note_lines(dir_rel, mds))
    lines.append("")

    target = os.path.join(dpath, "index.md") if dir_rel else os.path.join(_pool(), "index.md")
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
