#!/usr/bin/env python3
"""check_narrative_facts.py — narrative-facts verifier (audit round-2 E12/P1-2).

check_doc_references.py validates that docs point at things that exist
(links, CLI flags). It cannot see that a doc's *prose* describes the code
that is actually there. This script adds three narrative-fact checks, each
extracted from the code and asserted against the doc that owns the fact:

  A. skill catalog  — every slug-like skill name in okf/skills/skills.ava.okf.md
     functional-group rows must resolve under ava_builtins/skills/ or
     ava_builtins/plugins/ (catches a removed skill still listed — the
     2026-08-03 telegram residual class).
  B. IM command set — every `/command` route in services/im_bridge/core.py
     must appear in the "Command set" line of
     services/gateway_side/im_bridge.ava.okf.md (catches the 8/2-8/6 drift
     class: /spawn /commands /notice shipped, doc listed four commands).
  C. IM channels    — every adapter in services/im_bridge/adapters/ must be
     mentioned in im_bridge.ava.okf.md (channel aliases: weixin/wechat,
     feishu/lark). Catches "three channels" narrative after convergence.

Facts are declared once in code; the docs are views and this script is the
freshness check. Run from the repo root; wired into pre-commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CHANNEL_ALIASES = {
    "weixin": ("weixin", "wechat"),
    "feishu": ("feishu", "lark"),
}


def _skill_group_names(doc: str) -> list[str]:
    names: list[str] = []
    for line in doc.splitlines():
        if not line.startswith("- [["):
            continue
        if " — " not in line:
            continue
        after = line.split(" — ", 1)[1]
        for token in after.split("/"):
            t = token.strip()
            if SLUG.match(t):
                names.append(t)
    return names


def check_skill_catalog() -> list[str]:
    doc_path = ROOT / "okf/skills/skills.ava.okf.md"
    doc = doc_path.read_text(errors="replace")
    problems = []
    for name in _skill_group_names(doc):
        if name in ("comms", "ops_lifecycle", "orchestration", "self_improvement", "web_media"):
            continue  # group nodes, not skills
        if not (
            (ROOT / f"ava_builtins/skills/{name}").is_dir()
            or (ROOT / f"ava_builtins/plugins/{name}").is_dir()
            or (ROOT / f"ava_builtins/skills/{name}.ava.okf.md").is_file()
        ):
            problems.append(
                f"okf/skills/skills.ava.okf.md lists skill `{name}` but no "
                f"ava_builtins/skills/{name}/ or ava_builtins/plugins/{name}/ exists — "
                f"removed skill still in the catalog?"
            )
    return problems


def check_im_command_set() -> list[str]:
    core = (ROOT / "services/im_bridge/core.py").read_text(errors="replace")
    cmds = sorted(
        set(re.findall(r'cmd == "/([a-z]+)"', core) + re.findall(r'startswith\("/([a-z]+)"', core))
    )
    doc_path = ROOT / "services/gateway_side/im_bridge.ava.okf.md"
    doc = doc_path.read_text(errors="replace")
    cmd_line = next((ln for ln in doc.splitlines() if "Command set" in ln), "")
    missing = [c for c in cmds if f"/{c}" not in cmd_line]
    return [
        f"{doc_path} Command set line lacks shipped command /{c} "
        f"(present in services/im_bridge/core.py) — narrative drift"
        for c in missing
    ]


def check_im_channels() -> list[str]:
    adapters_dir = ROOT / "services/im_bridge/adapters"
    doc = (ROOT / "services/gateway_side/im_bridge.ava.okf.md").read_text(errors="replace")
    lower = doc.lower()
    problems = []
    if not adapters_dir.is_dir():
        return problems
    for f in sorted(adapters_dir.iterdir()):
        if not f.name.endswith(".py") or f.name == "__init__.py":
            continue
        stem = f.name[:-3]
        aliases = CHANNEL_ALIASES.get(stem, (stem,))
        if not any(a in lower for a in aliases):
            problems.append(
                f"{adapters_dir}/{f.name} exists but the channel is not mentioned in "
                f"services/gateway_side/im_bridge.ava.okf.md — "
                f"channel-convergence narrative drift"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    problems += check_skill_catalog()
    problems += check_im_command_set()
    problems += check_im_channels()
    if problems:
        print(f"check_narrative_facts: {len(problems)} narrative-fact violation(s)")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("check_narrative_facts: 0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
