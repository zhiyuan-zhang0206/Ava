import os
import re

import yaml

from meiva_builtins.plugins.meiva_memory.pool_ops import pool_dir


def _pool() -> str:
    return str(pool_dir())


RESERVED = {"MEMORY.md", "AGENTS.md", "index.md", "log.md"}


# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/docs/rebuild-memory-index.meiva.okf.md
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


# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/docs/rebuild-memory-index.meiva.okf.md
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
# meiva:note
# The module rewrites MEMORY.md in place: everything up to the exact `## Pointers` heading line is preserved (a prose mention does not match), followed by one `- [title](rel) — description` line per root note and one `- [dir/](dir/index.md) — n notes` line per topic directory with subdirectory counts; the index cap is enforced by the pool's pre-commit hook (MEIVA_MEMORY_INDEX_CAP, default 16000), not by this script.
# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/docs/rebuild-memory-index.meiva.okf.md
heading = re.search(r"^## Pointers[ \t]*$", content, re.M)
head = content[: heading.start()] if heading else (content.rstrip("\n") + "\n\n").lstrip("\n")
new = head + "## Pointers\n\n\n" + "\n".join(lines) + "\n"
print("root:", len(root_files), "dirs:", len(sub), "| chars:", len(new))
with open(os.path.join(_pool(), "MEMORY.md"), "w", encoding="utf-8") as f:
    f.write(new)
