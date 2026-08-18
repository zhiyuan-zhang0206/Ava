"""
Codebase Sweep Lite — ~11 agents, 5 waves, checkpoint-based joins.

Scans ~/Ava for legacy code patterns. Workers write their result file —
none of them messages the orchestrator. Each wave arms
one checkpoint (a gather_files watcher) and goes idle. Wave 5 arms no
checkpoint: the publisher serves the UI itself and nothing waits on it.

Contrast with codebase_sweep_orchestrator.py (7 waves, ~28 agents).
"""
# ruff: noqa: ANN201, DTZ005, PTH123

import json
from datetime import datetime
from pathlib import Path

import ava

ORCH = ava.self.AGENT_ID
HD = Path.home() / ".ava/workspaces" / str(ORCH) / "codebase_sweep_lite"
HD.mkdir(parents=True, exist_ok=True)
PF = HD / "progress.md"
SF = HD / "orchestrator_state.json"
REPO = Path.home() / "Ava"


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
# WAVE 1 — Scout (3 agents)
# ══════════════════════════════════════════════════════════════════

W1_TARGETS = [
    ("scout-shared", "shared/", "shared utilities, config, db layer"),
    ("scout-ava", "ava/", "SDK namespace, agent runtime"),
    ("scout-agent", "agent/", "agent loop, graph, execution"),
]

W1_FILES = [f"w1_{role}.json" for role, _, _ in W1_TARGETS]


def wave1():
    log("=" * 50)
    log("WAVE 1/5: Scout — 3 agents scanning codebase")
    log(f"Target: {REPO}")

    for fname in W1_FILES:
        (HD / fname).unlink(missing_ok=True)

    for role, directory, desc in W1_TARGETS:
        wid = spawn(
            f"""You are a Codebase Scout Worker.

Scan {REPO}/{directory} ({desc}) for:

1. **Stale TODOs**: grep for TODO/FIXME/HACK comments (ava.shell.run)
2. **Dead imports**: imports of non-existent or deprecated modules
3. **Python 3.10 compatibility code**: from __future__ import annotations etc.
4. **Dead code**: functions/classes suspected of being unused

Method:
1. First use ava.shell.run("cd {REPO} && grep -rn 'TODO' {directory} | head -30") to check for TODOs
2. Use ava.files.read to verify suspicious files
3. Allow false positives

Return JSON written to {hf(1, role)}:
{{
  "directory": "{directory}",
  "findings": [
    {{
      "type": "stale_todo|dead_import|py310_compat|dead_code",
      "file": "relative path",
      "line": line_number,
      "snippet": "code snippet",
      "confidence": "high|medium|low"
    }}
  ]
}}
After writing, message no one — the file IS the handoff.
""",
            label=role,
        )
        log(f"  [{role}] spawned #{wid} — {desc}")

    launch_checkpoint(W1_FILES, "w1-scout")
    write_state({"wave": 1, "next": 2, "spawned": len(W1_TARGETS)})
    ava.self.pause_heartbeat(600)
    log("Waiting for scouts...")


# ══════════════════════════════════════════════════════════════════
# WAVE 2 — Verify (4 agents)
# ══════════════════════════════════════════════════════════════════


def wave2():
    log("=" * 50)
    log("WAVE 2/5: Verify — cross-check findings")

    # Collect findings
    all_findings = []
    for role, _, _ in W1_TARGETS:
        fpath = hf(1, role)
        if fpath.exists():
            data = json.loads(ava.files.read(str(fpath)))
            all_findings.extend(data.get("findings", []))

    log(f"  Wave 1: {len(all_findings)} findings")

    if not all_findings:
        log("  No findings — skipping verify")
        write_state({"wave": 2, "next": 3, "spawned": 0})
        return

    # Group by type
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

            wid = spawn(
                f"""You are a Codebase Verify Worker ({ftype}).

Verify the following findings. For each:
1. Use ava.files.read to confirm the code exists
2. Determine whether it's a real issue or false positive
3. For dead_import, check the TYPE_CHECKING guard

Findings: {json.dumps(subset, ensure_ascii=False, indent=2)}

Return JSON written to {hf(2, role)}:
{{
  "role": "{role}",
  "verified": [{{"file":"...","line":...,"verdict":"confirmed|disputed"}}],
  "false_positives": [{{"file":"...","line":...,"note":"reason"}}]
}}
After writing, message no one — the file IS the handoff.
""",
                label=role,
            )
            log(f"  [{role}] spawned #{wid} — {len(subset)} findings")

    launch_checkpoint(W2_FILES, "w2-verify")
    write_state({"wave": 2, "next": 3, "spawned": len(W2_FILES)})
    ava.self.pause_heartbeat(900)
    log(f"Waiting for {len(W2_FILES)} verifiers...")


# ══════════════════════════════════════════════════════════════════
# WAVE 3 — Reduce (1 agent)
# ══════════════════════════════════════════════════════════════════


def wave3():
    log("=" * 50)
    log("WAVE 3/5: Reduce — draft report")

    wid = spawn(
        f"""You are a Sweep Report Writer.

Read:
- Scout findings: {HD}/w1_*.json
- Verify results: {HD}/w2_*.json

Generate a Markdown report:
# Codebase Sweep Report
## Summary
## Confirmed Issues (P0/P1/P2 priorities)
## Disputed / False Positives

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
    log("WAVE 4/5: Adversarial — challenge findings")

    draft_path = hf(3, "draft")
    draft_text = ""
    if draft_path.exists():
        draft = json.loads(ava.files.read(str(draft_path)))
        draft_text = draft.get("report", json.dumps(draft, ensure_ascii=False))

    W4_FILES = []
    for role, focus in [
        (
            "adversarial-fp",
            "Verify if each finding is really a problem — it might be expected behavior",
        ),
        (
            "adversarial-missed",
            "Scan shared/config.py and agent/loop.py to see what the scouts missed",
        ),
    ]:
        fname = f"w4_{role}.json"
        W4_FILES.append(fname)
        wid = spawn(
            f"""You are an Adversarial Reviewer: {role}.

{focus}

Review the report:
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
# WAVE 5 — Final Report + Publish (1 agent)
# ══════════════════════════════════════════════════════════════════


def wave5():
    log("=" * 50)
    log("WAVE 5/5: Final Report + Publish")

    wid = spawn(
        f"""You are the Final Publisher.

Read all results:
- {HD}/w1_*.json
- {HD}/w2_*.json
- {HD}/w3_draft.json
- {HD}/w4_*.json

Integrate all findings and review opinions, then generate the final Markdown report.

Use ava.ui.serve_markdown(content, name="codebase-sweep-lite", title="Codebase Sweep Report")
to display the final report.

After completion, message no one.
""",
        label="publisher",
    )
    log(f"  [publisher] spawned #{wid}")

    # No checkpoint for the publisher — it writes no result file, it serves the
    # UI directly, and no later wave consumes its output.
    write_state({"wave": 5, "next": None, "spawned": 1})
    ava.self.pause_heartbeat(300)
    log("Publisher launched. Report will appear in UI when ready.")


# ══════════════════════════════════════════════════════════════════
# Dispatch
# ══════════════════════════════════════════════════════════════════

WAVES = {1: wave1, 2: wave2, 3: wave3, 4: wave4, 5: wave5}


def run():
    state = read_state()
    current = state["wave"]

    if current not in WAVES:
        log("=" * 50)
        log(f"ALL DONE! Results in {HD}")
        return

    log(f"Resuming from wave {current}")
    WAVES[current]()


if __name__ == "__main__":
    log("Codebase Sweep Lite — 5 waves, checkpoint-based orchestration")
    log(f"Target: {REPO}")
    run()
