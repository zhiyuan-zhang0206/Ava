import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ava_home

mp = str(ava_home() / "memory")
RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}

# Directory-level index (2026-08-06 user ruling): MEMORY.md points at root
# entry notes (full desc, never truncated) and at directories — links get
# deeper as structure does; the cap stays 20000 untouched.


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
    return sorted(files)


def dir_notes(files):
    dirs = {}
    for rel in files:
        d = os.path.dirname(rel)
        dirs.setdefault(d, []).append(rel)
    return dirs


files = collect()
root_files = [f for f in files if "/" not in f]
dirs = dir_notes(files)

# --- root entry notes: one line each, full desc ---
lines = []
for rel in root_files:
    fm = frontmatter(rel)
    title = str(fm.get("title") or os.path.basename(rel)[:-3])
    desc = str(fm.get("description") or "").replace("\n", " ")
    lines.append(f"- [{title}]({rel}) \u2014 {desc}")

# --- directories: one line per dir with notes (full subdir breakdown) ---
sub = {}
for d in sorted(dirs):
    if not d:
        continue
    parts = d.split("/")
    if len(parts) == 1:
        sub.setdefault(d, []).append(d)
    else:
        sub.setdefault(parts[0], []).append(d)

for top in sorted(sub):
    members = [f for f in files if f.startswith(top + "/")]
    n = len(members)
    children = sub[top]
    if children == [top]:
        detail = f"{n} notes"
    else:
        counts = []
        for c in children:
            if c == top:
                continue
            cn = len([f for f in files if f.startswith(c + "/")])
            counts.append(f"{c.split('/')[-1]}({cn})")
        detail = f"{n} notes: {', '.join(counts)}"
    lines.append(f"- [{top}/]({top}/index.md) \u2014 {detail}")

with open(os.path.join(mp, "MEMORY.md"), encoding="utf-8") as f:
    content = f.read()
head = content[: content.find("## Pointers")]
new = head + "## Pointers\n\n\n" + "\n".join(lines) + "\n"
print("root:", len(root_files), "dirs:", len(sub), "| chars:", len(new))
with open(os.path.join(mp, "MEMORY.md"), "w", encoding="utf-8") as f:
    f.write(new)
