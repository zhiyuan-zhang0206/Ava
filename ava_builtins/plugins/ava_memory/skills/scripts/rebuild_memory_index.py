import os
import re

import yaml

from ava_builtins.plugins.ava_memory.pool_ops import pool_dir


def _pool() -> str:
    return str(pool_dir())


RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


# ava:ref ava_builtins/plugins/ava_memory/skills/scripts/docs/rebuild-memory-index.ava.okf.md#indexed-entries
def frontmatter(rel):
    with open(os.path.join(_pool(), rel), encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


# ava:ref ava_builtins/plugins/ava_memory/skills/scripts/docs/rebuild-memory-index.ava.okf.md#indexed-entries
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

lines = []
for rel in root_files:
    fm = frontmatter(rel)
    title = str(fm.get("title") or os.path.basename(rel)[:-3])
    desc = str(fm.get("description") or "").replace("\n", " ")
    lines.append(f"- [{title}]({rel}) \u2014 {desc}")

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

memory_path = os.path.join(_pool(), "MEMORY.md")
content = ""
if os.path.exists(memory_path):
    with open(memory_path, encoding="utf-8") as f:
        content = f.read()
# ava:ref ava_builtins/plugins/ava_memory/skills/scripts/docs/rebuild-memory-index.ava.okf.md#pointer-rebuild
heading = re.search(r"^## Pointers[ \t]*$", content, re.M)
head = content[: heading.start()] if heading else (content.rstrip("\n") + "\n\n").lstrip("\n")
new = head + "## Pointers\n\n\n" + "\n".join(lines) + "\n"
print("root:", len(root_files), "dirs:", len(sub), "| chars:", len(new))
with open(os.path.join(_pool(), "MEMORY.md"), "w", encoding="utf-8") as f:
    f.write(new)
