"""
Codebase Sweep: Legacy Code & Stale Patterns
=============================================

Complete orchestrator reference script — explore→fork→join→reduce pattern for codebase scanning.

Completion protocol: silent workers, file handoff
  Worker completes → writes JSON file to handoff directory, silently
  Orchestrator → arms one checkpoint per wave (gather_files watcher) → the
  checkpoint's files land → the orchestrator wakes once and runs the next wave

Checkpoints: one per wave, because every wave consumes the previous wave's
output.  W5 counts revision files by glob (only the critiques that map to a
live Wave-2 agent produce one); W7 wakes on the two reviews, not the publisher.

Execution: each execute_code runs one wave. Same state machine pattern as deep_research_orchestrator.

Wave structure:
  W1 — Scout   (4 agents)  →  scan 4 directories for legacy patterns
  W2 — Verify  (8 agents)  →  cross-verify W1 findings
  W3 — Reduce  (1 agent)   →  draft report
  W4 — Adversarial (4)     →  challenge findings
  W5 — Feedback            →  ♻️ feed adversarial results back to W2 agents
  W6 — Reduce  (1 agent)   →  final report
  W7 — Publish (3 agents)  →  review + publish

Total: ~28 agents / 7 waves
"""
# ruff: noqa: ANN201, DTZ005, PTH123

import json
from datetime import datetime
from pathlib import Path

import ava

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════
ORCHESTRATOR = ava.self.AGENT_ID
TASK = "codebase_sweep"
HANDOFF = Path.home() / ".ava/workspaces" / str(ORCHESTRATOR) / TASK
HANDOFF.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = HANDOFF / "progress.md"
STATE_FILE = HANDOFF / "orchestrator_state.json"
REPO_ROOT = Path.home() / "Ava"

# Agent registry: used to recall original agents during feedback loop
registry: dict[int, dict] = {}  # wid → {wave, role, label}

# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def hf(wave: int, role: str) -> Path:
    return HANDOFF / f"w{wave}_{role}.json"


def spawn(prompt: str, label: str, wave: int, role: str) -> int:
    wid = ava.agents.spawn(prompt=prompt, label=label)
    registry[wid] = {"wave": wave, "role": role, "label": label}
    return wid


def read_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(ava.files.read(str(STATE_FILE)))
    return {"wave": 1, "phase": "scout"}


def write_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def launch_checkpoint(
    expected_files: list[str],
    wave_name: str,
    timeout: str = "15m",
    required_count: int = 0,
    match_glob: str = "",
) -> None:
    """Arm this wave's checkpoint — the only thing that wakes the orchestrator.

    Reads gather_files.py at runtime and patches its placeholders.
    `required_count` > 0 wakes at K of N; `match_glob` counts by pattern when
    the result names are not all known here.
    """
    skill_path = str(ava.skills.ava_dynamic_workflow.path)
    code = ava.files.read(f"{skill_path}/reference/gather_files.py")
    code = code.replace('HANDOFF_DIR = ""', f'HANDOFF_DIR = "{HANDOFF}"')
    code = code.replace(
        "EXPECTED_FILES: list[str] = []",
        f"EXPECTED_FILES = {json.dumps(expected_files)}",
    )
    code = code.replace('MATCH_GLOB = ""', f'MATCH_GLOB = "{match_glob}"')
    code = code.replace("REQUIRED_COUNT = 0", f"REQUIRED_COUNT = {required_count}")
    code = code.replace("ORCHESTRATOR_ID = 0", f"ORCHESTRATOR_ID = {ORCHESTRATOR}")
    ava.watcher.launch(code, timeout=timeout, name=f"gather-{wave_name}")


def show_progress(wave: int, done: int, total: int, stage: str) -> None:
    bar = "#" * done + "." * (total - done)
    log(f"Wave {wave} [{stage}]  {bar}  {done}/{total}")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 1 — Scout (4 agents)
# ═══════════════════════════════════════════════════════════════════════════════

W1_TARGETS = [
    ("scout-agent", REPO_ROOT / "agent", "agent/"),
    ("scout-ava", REPO_ROOT / "ava", "ava/"),
    ("scout-shared", REPO_ROOT / "shared", "shared/"),
    ("scout-plugins", REPO_ROOT / "plugins", "plugins/"),
]

W1_FILES = [f"w1_{role}.json" for role, _, _ in W1_TARGETS]


def wave1():
    log("=" * 50)
    log("WAVE 1/7: Scout — 4 agents scanning 4 directories")
    log(f"Target: {REPO_ROOT}")
    log(f"Handoff: {HANDOFF}")

    for fname in W1_FILES:
        (HANDOFF / fname).unlink(missing_ok=True)

    for role, directory, desc in W1_TARGETS:
        out_path = hf(1, role)
        prompt = f"""You are a Codebase Scout Worker.

Scan {directory} ({desc}) for the following legacy code patterns:

1. **Stale TODOs**: grep for TODO/FIXME/HACK comments, noting date and file location
2. **Dead imports**: import statements that import non-existent modules
3. **Python 3.10 compatibility code** (target is already 3.12): e.g., `from __future__ import annotations`, etc.
4. **Dead code**: functions/classes that are defined but never called

Method:
1. Use ava.shell.run("cd {REPO_ROOT} && grep -rn 'TODO\\|FIXME\\|HACK' {desc}") to find TODOs
2. Use ava.files.read to verify key files
3. Allow false positives — Wave 2 will cross-verify

Write the resulting JSON to {out_path}:
{{
  "directory": "{desc}",
  "findings": [
    {{
      "type": "stale_todo|dead_import|py310_compat|dead_code",
      "file": "path/to/file.py",
      "line": 42,
      "snippet": "code or comment",
      "confidence": "high|medium|low"
    }}
  ]
}}
After writing, message no one — the file IS the handoff.
"""
        wid = spawn(prompt, role, wave=1, role=role)
        log(f"  [{role}] spawned #{wid} — scanning {desc}")

    show_progress(1, len(W1_TARGETS), len(W1_TARGETS), "spawned")
    launch_checkpoint(W1_FILES, "w1-scout")
    write_state({"wave": 1, "phase": "waiting", "next_wave": 2, "spawned": len(W1_TARGETS)})
    ava.self.pause_heartbeat(900)
    log("Waiting for all scouts...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 2 — Verify (up to 8 agents)
# ═══════════════════════════════════════════════════════════════════════════════


def wave2():
    log("=" * 50)
    log("WAVE 2/7: Verify — cross-verify Wave 1 findings")

    all_findings = []
    for role, _, _ in W1_TARGETS:
        fpath = hf(1, role)
        if fpath.exists():
            data = json.loads(ava.files.read(str(fpath)))
            all_findings.extend(data.get("findings", []))

    log(f"  Wave 1 produced {len(all_findings)} findings")

    if not all_findings:
        log("  No findings — all scouts may have failed")
        return

    by_type: dict[str, list] = {}
    for f in all_findings:
        by_type.setdefault(f["type"], []).append(f)

    W2_FILES = []
    for ftype, findings in by_type.items():
        half = max(1, len(findings) // 2)
        for suffix, subset in [("a", findings[:half]), ("b", findings[half:])]:
            role = f"verify-{ftype}-{suffix}"
            fname = f"w2_{role}.json"
            W2_FILES.append(fname)

            prompt = f"""You are a Codebase Verify Worker ({ftype}).

Verify the following findings. For each:
1. Use ava.files.read to open the file and confirm the code actually exists
2. Determine whether it is a real issue or a false positive
3. For dead_import, check if the import is inside a TYPE_CHECKING guard

Findings: {json.dumps(subset, ensure_ascii=False, indent=2)}

Write the resulting JSON to {hf(2, role)}:
{{
  "role": "{role}",
  "verified": [{{"file":"...", "line":..., "verdict":"confirmed|disputed"}}],
  "false_positives": [{{"file":"...", "line":..., "note":"why it is a false positive"}}],
  "uncertain": [{{"file":"...", "line":..., "note":"needs more information"}}]
}}
After writing, message no one — the file IS the handoff.
"""
            wid = spawn(prompt, role, wave=2, role=role)
            log(f"  [{role}] spawned #{wid} — {len(subset)} findings")

    show_progress(2, len(W2_FILES), len(W2_FILES), "spawned")
    launch_checkpoint(W2_FILES, "w2-verify")
    write_state({"wave": 2, "phase": "waiting", "next_wave": 3, "spawned": len(W2_FILES)})
    ava.self.pause_heartbeat(900)
    log(f"Waiting for {len(W2_FILES)} verifiers...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 3 — Reduce: draft report
# ═══════════════════════════════════════════════════════════════════════════════


def wave3():
    log("=" * 50)
    log("WAVE 3/7: Reduce — draft report")

    prompt = f"""You are a Sweep Report Writer.

Read the following files and generate a Markdown draft report:
- Scout findings: {HANDOFF}/w1_*.json
- Verify results: {HANDOFF}/w2_*.json

Report structure:
# Codebase Sweep Report
## Summary (overview statistics)
## Confirmed Issues
### P0 - Immediate Fix
### P1 - This Sprint
### P2 - Backlog
## Disputed / False Positives
## Uncategorized

Write to {hf(3, "draft")}, wrapped in JSON: {{"report":"markdown..."}}
After writing, message no one — the file IS the handoff.
"""
    spawn(prompt, "report-writer", wave=3, role="writer")
    launch_checkpoint(["w3_draft.json"], "w3-draft", "10m")
    write_state({"wave": 3, "phase": "waiting", "next_wave": 4, "spawned": 1})
    ava.self.pause_heartbeat(600)
    log("Waiting for draft...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 4 — Adversarial (4 agents)
# ═══════════════════════════════════════════════════════════════════════════════


def wave4():
    log("=" * 50)
    log("WAVE 4/7: Adversarial — challenge findings")

    draft_path = hf(3, "draft")
    if not draft_path.exists():
        log("  Draft not found")
        return

    draft = json.loads(ava.files.read(str(draft_path)))
    draft_text = draft.get("report", json.dumps(draft, ensure_ascii=False))

    W4_ROLES = [
        ("adversarial-fp", "Try to prove each confirmed finding is actually a false positive"),
        ("adversarial-severity", "Challenge severity ratings — should any P1 be P0? Any P0 be P2?"),
        ("adversarial-missed", "Find issues the scouts missed — scan a directory they didn't"),
        ("adversarial-contra", "Argue against each finding: why it might NOT be worth fixing"),
    ]

    W4_FILES = []
    for role, focus in W4_ROLES:
        fname = f"w4_{role}.json"
        W4_FILES.append(fname)
        prompt = f"""You are an Adversarial Reviewer: {role}.

{focus}

Examine the following report and identify >=3 specific problems:
{draft_text[:5000]}

Write the resulting JSON to {hf(4, role)}:
{{"role":"{role}","critiques":[{{"target":"...","issue":"...","suggestion":"..."}}]}}
After writing, message no one — the file IS the handoff.
"""
        spawn(prompt, role, wave=4, role=role)
        log(f"  [{role}] spawned")

    show_progress(4, len(W4_ROLES), len(W4_ROLES), "spawned")
    launch_checkpoint(W4_FILES, "w4-adversarial")
    write_state({"wave": 4, "phase": "waiting", "next_wave": 5, "spawned": len(W4_ROLES)})
    ava.self.pause_heartbeat(600)
    log("Waiting for adversarial reviews...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 5 — Feedback Loop
# ═══════════════════════════════════════════════════════════════════════════════


def wave5():
    log("=" * 50)
    log("WAVE 5/7: Feedback Loop — critiques back to verify agents")

    critiques_by_target: dict[str, list] = {}
    for fpath in HANDOFF.glob("w4_adversarial-*.json"):
        data = json.loads(ava.files.read(str(fpath)))
        for c in data.get("critiques", []):
            target = c.get("target", "unknown")
            critiques_by_target.setdefault(target, []).append(c)

    total = sum(len(v) for v in critiques_by_target.values())
    log(f"  {total} critiques across {len(critiques_by_target)} targets")

    feedback_count = 0
    for target, critiques in critiques_by_target.items():
        target_wids = [
            wid
            for wid, info in registry.items()
            if info["wave"] == 2 and target in info.get("role", "")
        ]
        if target_wids:
            wid = target_wids[0]
            critique_text = json.dumps(critiques, ensure_ascii=False, indent=2)
            ava.agents.resurrect(
                wid,
                prompt=f"""FEEDBACK: Your Wave 2 verification work received the following critiques.

{critique_text}

Please revise your judgment based on the new information:
1. Update your results file
2. ava.files.write("{hf(5, f"revision_{target}")}", <a JSON summary of what you changed>)
Message no one — the file IS the handoff.
""",
            )
            feedback_count += 1
            log(f"  Feedback -> #{wid} ({registry[wid]['role']}): {len(critiques)} critiques")

    # Checkpoint by glob + count: only the critiques that matched a live Wave-2
    # agent produce a revision file, so the names are not knowable up front.
    if feedback_count:
        launch_checkpoint(
            [], "w5-feedback", "15m", required_count=feedback_count, match_glob="w5_revision_*.json"
        )
    else:
        log("  No feedback dispatched — no checkpoint to arm, go straight to wave 6")
    write_state({"wave": 5, "phase": "waiting", "next_wave": 6, "feedback_count": feedback_count})
    ava.self.pause_heartbeat(900)
    log(f"Waiting for {feedback_count} revisions...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 6 — Final Reduce
# ═══════════════════════════════════════════════════════════════════════════════


def wave6():
    log("=" * 50)
    log("WAVE 6/7: Final Reduce")

    prompt = f"""You are the Final Report Writer.

Integrate all materials and write the final report:
- Scout findings: {HANDOFF}/w1_*.json
- Verify results: {HANDOFF}/w2_*.json
- Draft: {hf(3, "draft")}
- Adversarial reviews: {HANDOFF}/w4_*.json

Final report: only include verified true positives. Label as confirmed/disputed/withdrawn.
Write to {hf(6, "final")}, wrapped in JSON.
After writing, message no one — the file IS the handoff.
"""
    spawn(prompt, "final-writer", wave=6, role="writer")
    launch_checkpoint(["w6_final.json"], "w6-final", "10m")
    write_state({"wave": 6, "phase": "waiting", "next_wave": 7, "spawned": 1})
    ava.self.pause_heartbeat(600)
    log("Waiting for final report...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 7 — Publish
# ═══════════════════════════════════════════════════════════════════════════════


def wave7():
    log("=" * 50)
    log("WAVE 7/7: Publish — dual review + serve")

    final_path = hf(6, "final")
    if not final_path.exists():
        log("  Final report not found")
        return

    final = json.loads(ava.files.read(str(final_path)))
    final_text = final.get("report", json.dumps(final, ensure_ascii=False))

    for suffix in ["a", "b"]:
        prompt = f"""You are Final Reviewer {suffix.upper()}.

Review the final report and return {{"approved":true/false, "changes":[...]}}.

Report: {final_text[:5000]}

Write to {hf(7, f"review_{suffix}")}. Message no one.
"""
        spawn(prompt, f"reviewer-{suffix}", wave=7, role=f"reviewer-{suffix}")
        log(f"  [reviewer-{suffix}] spawned")

    publisher_prompt = """You are the Publisher.

Read both reviews, merge changes, render to self-contained HTML, and serve it with ava.ui.serve().
Title: "Ava Codebase Sweep Report"
Message no one — the file IS the handoff.
"""
    spawn(publisher_prompt, "publisher", wave=7, role="publisher")

    # Designated reporters: three agents run, only the two reviews gate the
    # wake-up. The publisher serves the UI itself and is never waited on.
    launch_checkpoint(["w7_review_a.json", "w7_review_b.json"], "w7-review")
    write_state({"wave": 7, "phase": "waiting", "next_wave": None, "spawned": 3})
    ava.self.pause_heartbeat(600)
    log("Waiting for reviews, then publisher serves report via UI.")


# ═══════════════════════════════════════════════════════════════════════════════
# Wave dispatch
# ═══════════════════════════════════════════════════════════════════════════════

WAVES = {
    1: wave1,
    2: wave2,
    3: wave3,
    4: wave4,
    5: wave5,
    6: wave6,
    7: wave7,
}


def run_current_wave():
    state = read_state()
    current = state["wave"]

    if current not in WAVES:
        log("All waves complete!")
        log(f"Results in: {HANDOFF}")
        return

    log(f"Resuming from wave {current}")
    WAVES[current]()


if __name__ == "__main__":
    log("Codebase Sweep Orchestrator")
    log(f"Target: {REPO_ROOT}")
    log("Total: 7 waves, ~28 agents")
    log(f"Handoff: {HANDOFF}")
    log("Each wave = one turn. Watcher wakes orchestrator for next wave.")
    run_current_wave()
