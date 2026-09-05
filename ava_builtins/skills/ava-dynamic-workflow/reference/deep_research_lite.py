"""
Deep Research Lite — AI Coding Agent 2026
==========================================
Scaled-down demo: ~11 agents, 5 waves, checkpoint-based joins.

Runs in a persistent shell session. Workers write their result file —
none of them messages the orchestrator. Each wave arms
one checkpoint (a gather_files watcher) and goes idle; the checkpoint wakes
the orchestrator once, and the next wave executes. Wave 5 arms no checkpoint:
the publisher serves the UI itself and nothing downstream waits on it.

Contrast with deep_research_orchestrator.py (7 waves, ~40 agents).
"""

import json
from datetime import datetime
from pathlib import Path

import ava

ORCH = ava.self.AGENT_ID
HD = Path.home() / ".ava/workspaces" / str(ORCH) / "deep_research_lite"
HD.mkdir(parents=True, exist_ok=True)
PF = HD / "progress.md"
SF = HD / "orchestrator_state.json"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(PF, "a") as f:
        f.write(line + "\n")


def hf(wave: int, role: str) -> Path:
    return HD / f"w{wave}_{role}.json"


def spawn(prompt: str, label: str = "") -> int:
    return ava.agents.spawn(prompt=prompt, label=label)


def read_state() -> dict:
    if SF.exists():
        return json.loads(ava.files.read(str(SF)))
    return {"wave": 1}


def write_state(state: dict) -> None:
    with open(SF, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def launch_checkpoint(
    expected_files: list[str],
    wave_name: str,
    timeout: str = "10m",
    required_count: int = 0,
) -> None:
    """Arm this wave's checkpoint. Patches the skill's gather_files.py reference.

    `required_count` > 0 wakes at K of N instead of waiting for every file.
    """
    skill_path = str(ava.skills.ava_dynamic_workflow.path)
    code = ava.files.read(f"{skill_path}/reference/gather_files.py")
    code = code.replace('HANDOFF_DIR = ""', f'HANDOFF_DIR = "{HD}"')
    code = code.replace(
        "EXPECTED_FILES: list[str] = []",
        f"EXPECTED_FILES = {json.dumps(expected_files)}",
    )
    code = code.replace("REQUIRED_COUNT = 0", f"REQUIRED_COUNT = {required_count}")
    code = code.replace("ORCHESTRATOR_ID = 0", f"ORCHESTRATOR_ID = {ORCH}")
    ava.watcher.launch(code, timeout=timeout, name=f"gather-{wave_name}")


# ══════════════════════════════════════════════════════════════════
# WAVE 1 — Explore (3 agents)
# ══════════════════════════════════════════════════════════════════

W1_ROLES = [
    ("explore-commercial", "Commercial Products: GitHub Copilot, Cursor, Claude Code, Codex"),
    ("explore-opensource", "Open Source Ecosystem: Aider, Continue, Cline"),
    ("explore-china", "China Market: Tongyi Lingma, Baidu Comate, MarsCode"),
]

W1_FILES = [f"w1_{role}.json" for role, _ in W1_ROLES]


def wave1():
    log("=" * 50)
    log("WAVE 1/5: Explore — 3 agents fan-out (web search)")

    for fname in W1_FILES:
        (HD / fname).unlink(missing_ok=True)

    for role, topic in W1_ROLES:
        wid = spawn(
            f"""You are Deep Research Worker — Wave 1 Explore.

Research topic: AI coding agent 2026 competitive landscape
Your dimension: {topic}

Use ava.web.search and ava.web.fetch to search and analyze this dimension.
Return 5-10 key findings, each containing claim/source/confidence.

After completion:
1. Write results to {hf(1, role)} using ava.files.write, in JSON format:
   {{"role":"{role}","claims":[{{"claim":"...","source":"url","confidence":"high|medium|low"}}]}}
Do not message anyone — writing the file IS the handoff.
""",
            label=role,
        )
        log(f"  [{role}] spawned #{wid}")

    launch_checkpoint(W1_FILES, "w1-explore")
    write_state({"wave": 1, "next": 2, "spawned": len(W1_ROLES)})
    ava.self.pause_heartbeat(900)
    log("Checkpoint armed. Going idle until Wave 1 completes.")


# ══════════════════════════════════════════════════════════════════
# WAVE 2 — Verify (3 agents)
# ══════════════════════════════════════════════════════════════════


def wave2():
    log("=" * 50)
    log("WAVE 2/5: Verify — 3 agents cross-check")

    W2_FILES = []
    for i in range(3):
        fname = f"w2_verify_{i + 1}.json"
        W2_FILES.append(fname)
        wid = spawn(
            f"""You are Deep Research Verifier #{i + 1}.

Read all claims from {HD}/w1_*.json, pick 2-3 for deep verification.
For each claim: search independent sources to confirm or refute.

Return JSON written to {hf(2, f"verify_{i + 1}")}:
{{
  "verifier": "verifier-{i + 1}",
  "verifications": [
    {{
      "claim": "original claim",
      "verdict": "confirmed|partially_confirmed|refuted",
      "evidence": [{{"source":"url","finding":"..."}}],
      "confidence": "high|medium|low"
    }}
  ]
}}
After writing, message no one — the file IS the handoff.
""",
            label=f"verifier-{i + 1}",
        )
        log(f"  [verifier-{i + 1}] spawned #{wid}")

    launch_checkpoint(W2_FILES, "w2-verify")
    write_state({"wave": 2, "next": 3, "spawned": len(W2_FILES)})
    ava.self.pause_heartbeat(900)
    log("Waiting for verifiers...")


# ══════════════════════════════════════════════════════════════════
# WAVE 3 — Reduce (1 agent)
# ══════════════════════════════════════════════════════════════════


def wave3():
    log("=" * 50)
    log("WAVE 3/5: Reduce — draft report")

    wid = spawn(
        f"""You are Research Report Writer.

Read:
- {HD}/w1_*.json (exploration findings)
- {HD}/w2_*.json (verification results)

Write a Markdown draft report, structure:
# AI Coding Agent 2026 Competitive Landscape
## Executive Summary
## 1. Market Overview
## 2. Key Findings (mark verified/likely/disputed)
## 3. Competitive Dynamics
## 4. Risks and Uncertainties

Write to {hf(3, "draft")}, wrapped in JSON: {{"report":"markdown..."}}
After writing, message no one — the file IS the handoff.
""",
        label="report-writer",
    )
    log(f"  [report-writer] spawned #{wid}")

    launch_checkpoint(["w3_draft.json"], "w3-draft", "10m")
    write_state({"wave": 3, "next": 4, "spawned": 1})
    ava.self.pause_heartbeat(600)
    log("Waiting for draft...")


# ══════════════════════════════════════════════════════════════════
# WAVE 4 — Adversarial (2 agents)
# ══════════════════════════════════════════════════════════════════


def wave4():
    log("=" * 50)
    log("WAVE 4/5: Adversarial — 2 agents challenge")

    draft_path = hf(3, "draft")
    draft_text = ""
    if draft_path.exists():
        draft = json.loads(ava.files.read(str(draft_path)))
        draft_text = draft.get("report", json.dumps(draft, ensure_ascii=False))

    W4_FILES = []
    for role in ["fact-checker", "gap-finder"]:
        fname = f"w4_{role}.json"
        W4_FILES.append(fname)
        focus = (
            "Verify whether the source of each claim is reliable and the data is reproducible."
            if role == "fact-checker"
            else "Find missing perspectives, companies, products, or risks."
        )
        wid = spawn(
            f"""You are Adversarial Reviewer: {role}.

{focus}

Review the following draft, find >=2 specific issues:

{draft_text[:4000]}

Return JSON written to {hf(4, role)}:
{{"role":"{role}","critiques":[{{"target":"...","issue":"...","suggestion":"..."}}]}}
After writing, message no one — the file IS the handoff.
""",
            label=role,
        )
        log(f"  [{role}] spawned #{wid}")

    launch_checkpoint(W4_FILES, "w4-adversarial")
    write_state({"wave": 4, "next": 5, "spawned": len(W4_FILES)})
    ava.self.pause_heartbeat(600)
    log("Waiting for adversarial reviews...")


# ══════════════════════════════════════════════════════════════════
# WAVE 5 — Final + Publish (1 agent)
# ══════════════════════════════════════════════════════════════════


def wave5():
    log("=" * 50)
    log("WAVE 5/5: Final Report + Publish")

    wid = spawn(
        f"""You are Final Publisher.

Read all result files:
- {HD}/w1_*.json (exploration)
- {HD}/w2_*.json (verification)
- {HD}/w3_draft.json (draft)
- {HD}/w4_*.json (adversarial reviews)

Generate the final Markdown report, integrating all findings and review comments.

Render the report to self-contained HTML, then serve it with
ava.ui.serve(page_dir, name="deep-research-lite", title="AI Coding Agent 2026 Competitive Landscape").

After completion, message no one.
""",
        label="publisher",
    )
    log(f"  [publisher] spawned #{wid}")

    # No checkpoint for the publisher — it writes no result file, it serves the
    # UI directly, and no later wave consumes its output.
    write_state({"wave": 5, "next": None, "spawned": 1})
    ava.self.pause_heartbeat(300)
    log("Publisher launched. Results will appear in UI when ready.")


# ══════════════════════════════════════════════════════════════════
# Dispatch
# ══════════════════════════════════════════════════════════════════

WAVES = {1: wave1, 2: wave2, 3: wave3, 4: wave4, 5: wave5}


def run():
    state = read_state()
    current = state["wave"]

    if current not in WAVES:
        total = sum(1 for p in HD.glob("w*_*.json"))
        log("=" * 50)
        log(f"ALL DONE! {total} result files in {HD}")
        return

    log(f"Resuming from wave {current}")
    WAVES[current]()


if __name__ == "__main__":
    log("Deep Research Lite — 5 waves, checkpoint-based orchestration")
    run()
