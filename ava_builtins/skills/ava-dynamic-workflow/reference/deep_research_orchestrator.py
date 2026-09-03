# ruff: noqa: ANN201, DTZ005, PTH123
"""
Deep Research: AI Coding Agent 2026 Competitive Landscape
========================================================

Orchestrator script — drop into Ava agent and run.

Completion protocol: silent workers, file handoff
  Worker finishes → writes JSON file to handoff directory, silently
  Orchestrator → arms one checkpoint per wave (gather_files watcher) → the
  checkpoint's files land → the orchestrator wakes once and runs the next wave

Checkpoints: one per wave, because every wave consumes the previous wave's
output.  W5 counts feedback files by glob (only the critiques that map to a
live agent produce one); W7 wakes on the two reviews, not on the publisher.

Progress tracking: console print + progress.md file

Wave structure:
  W1 — Explore  (5 agents)  →  5 dimensions parallel search
  W2 — Deep-dive (10 agents) →  verify 10 key claims
  W3 — Reduce   (1 agent)   →  first draft
  W4 — Adversarial (5)      →  find flaws
  W5 — Feedback (15 agents) →  ♻️ feed back to W1+W2 original agents
  W6 — Reduce   (1 agent)   →  final draft
  W7 — Publish  (3 agents)  →  review + publish

Total: ~40 agents / 7 waves
"""

import json
from datetime import datetime
from pathlib import Path

import ava

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════
ORCHESTRATOR = ava.self.AGENT_ID
TASK = "deep-research"
HANDOFF = Path.home() / ".ava/workspaces" / str(ORCHESTRATOR) / TASK
HANDOFF.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = HANDOFF / "progress.md"

# Agent registry: retrieve original agent during feedback loop
registry: dict[int, dict] = {}  # wid → {wave, role, label}

# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════


def log(msg: str) -> None:
    """Progress: print + activity log + progress file."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def hf(wave: int, role: str) -> Path:
    """Handoff file path: w{wave}_{role}.json"""
    return HANDOFF / f"w{wave}_{role}.json"


def spawn(prompt: str, label: str, wave: int, role: str) -> int:
    wid = ava.agents.spawn(prompt=prompt, label=label)
    registry[wid] = {"wave": wave, "role": role, "label": label}
    return wid


def launch_checkpoint(
    expected_files: list[str],
    wave_name: str,
    timeout: str = "15m",
    required_count: int = 0,
    match_glob: str = "",
) -> None:
    """Arm this wave's checkpoint — the only thing that wakes the orchestrator.

    Reads gather_files.py from the skill's reference directory at runtime,
    replaces the placeholders with actual paths, and launches it as a watcher.
    `required_count` > 0 wakes at K of N; `match_glob` counts by pattern when
    the result names are not all known here.
    """
    skill_path = str(ava.skills.ava_dynamic_workflow.path)
    code = ava.files.read(f"{skill_path}/reference/gather_files.py")
    code = code.replace('HANDOFF_DIR = ""', f'HANDOFF_DIR = "{HANDOFF}"')
    code = code.replace(
        "EXPECTED_FILES: list[str] = []", f"EXPECTED_FILES = {json.dumps(expected_files)}"
    )
    code = code.replace('MATCH_GLOB = ""', f'MATCH_GLOB = "{match_glob}"')
    code = code.replace("REQUIRED_COUNT = 0", f"REQUIRED_COUNT = {required_count}")
    code = code.replace("ORCHESTRATOR_ID = 0", f"ORCHESTRATOR_ID = {ORCHESTRATOR}")
    ava.watcher.launch(code, timeout=timeout, name=f"gather-{wave_name}")


def show_progress(wave: int, done: int, total: int, stage: str) -> None:
    bar = "█" * done + "░" * (total - done)
    log(f"Wave {wave} [{stage}]  {bar}  {done}/{total} agents done")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 1 — Explore (5 agents, file handoff)
# ═══════════════════════════════════════════════════════════════════════════════

W1 = [
    (
        "explore-commercial",
        "Commercial Products",
        "GitHub Copilot, Cursor, Claude Code, Codex, Replit Agent",
    ),
    ("explore-opensource", "Open Source Ecosystem", "Aider, Continue, Cline, OpenHands, SWE-Agent"),
    (
        "explore-china",
        "Chinese Market",
        "\u901a\u4e49\u7075\u7801, Baidu Comate, \u8c46\u5305MarsCode, \u817e\u8baf\u4e91AI\u4ee3\u7801\u52a9\u624b",
    ),
    (
        "explore-academic",
        "Academic Frontier",
        "SWE-bench, AgentBench, latest papers, evaluation benchmarks",
    ),
    ("explore-enterprise", "Enterprise Adoption", "Fortune 500 cases, ROI data, deployment scale"),
]

W1_FILES = [f"w1_{role}.json" for role, _, _ in W1]

log("=" * 50)
log("WAVE 1/7: Explore — 5 agents parallel search")
log(f"Handoff: {HANDOFF}")
log(f"Strategy: each worker writes {HANDOFF}/w1_<role>.json")

for role, topic_cn, topic_detail in W1:
    prompt = f"""You are a Deep Research Worker — Wave 1 Explore.

Research topic: AI coding agent 2026 competitive landscape
Your dimension: {topic_cn} ({topic_detail})

MOCK data (use ava.web.search + ava.web.fetch in production):
For this dimension, return 3-5 key findings, each containing:
- claim: core viewpoint
- source: source (fabricated but reasonable URL)
- confidence: high/medium/low

Completion protocol:
1. ava.files.write("{hf(1, role)}", json.dumps({{"role":"{role}","topic":"{topic_cn}","claims":[...]}}))
Do not message anyone — writing the file IS the handoff.
"""
    wid = spawn(prompt, role, wave=1, role=role)
    log(f"  [{role}] spawned #{wid}")

show_progress(1, 5, 5, "spawned")
log("→ Arm the wave-1 checkpoint...")
launch_checkpoint(W1_FILES, "w1-explore")
ava.self.pause_heartbeat(900)
log("Waiting for Wave 1 workers to finish. Will wake on watcher signal.")

# ═══════════════════════════════════════════════════════════════════════════════
# NOTE: The above is the complete executable code for Wave 1.
# The code for Waves 2-7 follows; in practice they execute sequentially after
# the orchestrator is woken by the watcher. For demo completeness they are all
# included here. The orchestrator will idle between waves.
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 2 — Deep-dive (1 planner → fork 10 verifiers)
# ═══════════════════════════════════════════════════════════════════════════════
# This section executes after the watcher wakes the orchestrator


def wave2():
    log("=" * 50)
    log("WAVE 2/7: Deep-dive — read Wave 1, plan 10 verification tasks")

    # Read all Wave 1 results
    all_claims = []
    for fname in W1_FILES:
        data = json.loads(ava.files.read(str(HANDOFF / fname)))
        all_claims.extend(data.get("claims", []))
    log(f"  Wave 1 output: {len(all_claims)} claims")

    # Planner agent: identify the 10 claims most worth verifying
    planner_prompt = f"""You are a Deep Research Planner.
Read the following {len(all_claims)} claims and select the 10 most in need of deep verification
(prioritize those with confidence=low, contradictory, or quantified data).
Return JSON: {{"to_verify": [{{"id":1,"claim":"...","reason":"why verification is needed"}}]}}
Claims: {json.dumps(all_claims, ensure_ascii=False)}
After completion, write to {hf(2, "planner")}. Message no one.
"""
    spawn(planner_prompt, "deep-dive-planner", wave=2, role="planner")
    launch_checkpoint(["w2_planner.json"], "w2-planner", "5m")
    ava.self.pause_heartbeat(300)
    log("Waiting for planner...")

    # ---- resume after wake ----
    planner_data = json.loads(ava.files.read(str(hf(2, "planner"))))
    to_verify = planner_data["to_verify"]
    log(f"  Planner selected {len(to_verify)} claims to verify")

    W2_FILES = []
    for item in to_verify:
        cid = item["id"]
        claim_text = item["claim"]
        fname = f"w2_verify_{cid}.json"
        W2_FILES.append(fname)

        prompt = f"""You are a Deep Research Verifier.
Verify this claim: "{claim_text}"
Method: search 2-3 independent sources to confirm or refute.
Return JSON: {{"claim_id":{cid},"claim":"{claim_text}","verdict":"confirmed|partially_confirmed|refuted","evidence":[...],"confidence":"high|medium|low"}}
Write to {hf(2, f"verify_{cid}")}. Message no one.
"""
        wid = spawn(prompt, f"verify-{cid}", wave=2, role=f"verify-{cid}")
        log(f"  [verify-{cid}] spawned #{wid} | claim: {claim_text[:80]}...")

    show_progress(2, len(to_verify), len(to_verify), "spawned")
    launch_checkpoint(W2_FILES, "w2-verify")
    ava.self.pause_heartbeat(900)
    log(f"Waiting for {len(to_verify)} verifiers...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 3 — Reduce: synthesize first draft
# ═══════════════════════════════════════════════════════════════════════════════


def wave3():
    log("=" * 50)
    log("WAVE 3/7: Reduce — synthesize first draft")

    # Read Wave 1 + Wave 2
    w1_data = {}
    for fname in W1_FILES:
        data = json.loads(ava.files.read(str(HANDOFF / fname)))
        w1_data[data["role"]] = data

    w2_data = []
    for fname in HANDOFF.glob("w2_verify_*.json"):
        w2_data.append(json.loads(ava.files.read(str(fname))))

    prompt = f"""You are a Research Report Writer.
Based on the following data, write a first draft report:

Wave 1 exploratory findings: {json.dumps(w1_data, ensure_ascii=False)}
Wave 2 verification results: {json.dumps(w2_data, ensure_ascii=False)}

Report structure:
1. Executive Summary (3 sentences)
2. Market Panorama (overview of 5 dimensions)
3. Key Findings (10 items, marked verified/likely/disputed)
4. Competitive Landscape Matrix (comparison table)
5. Risks and Uncertainties

Write to {hf(3, "draft")}. Message no one.
"""
    spawn(prompt, "report-writer", wave=3, role="writer")
    launch_checkpoint(["w3_draft.json"], "w3-draft", "10m")
    ava.self.pause_heartbeat(600)
    log("Waiting for draft...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 4 — Adversarial: 5 agents find flaws
# ═══════════════════════════════════════════════════════════════════════════════


def wave4():
    log("=" * 50)
    log("WAVE 4/7: Adversarial — 5 agents find flaws")

    draft = json.loads(ava.files.read(str(hf(3, "draft"))))

    W4_ROLES = [
        (
            "adversarial-fact",
            "Fact check: Are the sources for each claim reliable? Is the data reproducible?",
        ),
        (
            "adversarial-logic",
            "Logic check: Are there leaps in the reasoning chain? Is causality established?",
        ),
        (
            "adversarial-gap",
            "Gap check: What important perspectives are missing? Which companies/products have been overlooked?",
        ),
        ("adversarial-bias", "Bias check: Is there Western centrism? Is it overly optimistic?"),
        (
            "adversarial-contra",
            "Counter-argument: If you were a competitor, how would you refute this report?",
        ),
    ]

    W4_FILES = []
    for role, focus in W4_ROLES:
        fname = f"w4_{role}.json"
        W4_FILES.append(fname)
        prompt = f"""You are an Adversarial Reviewer.
{role}: {focus}
Review the following report draft and find at least 3 specific issues.
Each issue must: point out specific paragraph/claim, explain why it's problematic, give revision suggestion.
Format: {{"critiques":[{{"target":"claim_id/paragraph","issue":"...","suggestion":"..."}}]}}
Report: {json.dumps(draft, ensure_ascii=False)[:5000]}
Write to {hf(4, role)}. Message no one.
"""
        spawn(prompt, role, wave=4, role=role)
        log(f"  [{role}] spawned")

    show_progress(4, 5, 5, "spawned")
    launch_checkpoint(W4_FILES, "w4-adversarial")
    ava.self.pause_heartbeat(600)
    log("Waiting for adversarial reviews...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 5 — ♻️ FEEDBACK LOOP: feed adversarial results back to original agents
# ═══════════════════════════════════════════════════════════════════════════════


def wave5():
    log("=" * 50)
    log("WAVE 5/7: ♻️  Feedback Loop — feed adversarial results back to original agents")

    # Read all adversarial results
    all_critiques: dict[str, list] = {}  # role → critiques
    for fname in HANDOFF.glob("w4_adversarial-*.json"):
        data = json.loads(ava.files.read(str(fname)))
        for c in data.get("critiques", []):
            target = c.get("target", "unknown")
            all_critiques.setdefault(target, []).append(c)

    log(f"  Total {len(all_critiques)} critiques, involving {len(all_critiques)} targets")

    feedback_count = 0
    for target, critiques in all_critiques.items():
        # ♻️ Retrieve original agent: find the agent responsible for this target in registry
        target_wids = [
            wid
            for wid, info in registry.items()
            if info["role"] == target or target in info.get("role", "")
        ]

        if target_wids:
            wid = target_wids[0]
            critique_text = json.dumps(critiques, ensure_ascii=False, indent=2)
            result = ava.agents.resurrect(
                wid,
                prompt=f"""♻️ FEEDBACK: Your previous work in Wave {registry[wid]["wave"]} received the following critiques.
Please revise your judgment based on new information.

Critiques:
{critique_text}

After revision:
1. Update your original result file
2. ava.files.write("{hf(5, f"feedback_{target}")}", <a JSON summary of what you changed>)
Do not message anyone — writing the file IS the handoff.
""",
            )
            log(f"  ♻️  Feedback → #{wid} ({registry[wid]['role']}): {len(critiques)} critiques")
            feedback_count += 1
        else:
            log(f"  ⚠️  Target '{target}' not found in registry, spawning new agent")
            # Fallback: spawn new agent with fork context
            # (simplified in demo)

    show_progress(5, feedback_count, len(all_critiques), "feedback sent")
    # Checkpoint by glob + count: only the critiques that mapped to a live agent
    # produce a file, so the file names are not knowable from all_critiques.
    if feedback_count:
        launch_checkpoint(
            [], "w5-feedback", "15m", required_count=feedback_count, match_glob="w5_feedback_*.json"
        )
    else:
        log("  No feedback dispatched — no checkpoint to arm, go straight to wave 6")
    ava.self.pause_heartbeat(900)
    log(f"Waiting for {feedback_count} feedback revisions...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 6 — Reduce: final draft
# ═══════════════════════════════════════════════════════════════════════════════


def wave6():
    log("=" * 50)
    log("WAVE 6/7: Final Reduce — integrate all revisions, produce final draft")

    prompt = f"""You are a Final Report Writer.
Integrate all the following materials to write the final report:
- First draft: {hf(3, "draft")}
- Adversarial reviews: {HANDOFF}/w4_*.json
- Revised feedback: {HANDOFF}/w5_*.json

Final report format (Markdown):
# AI Coding Agent 2026 Competitive Landscape Report
## Executive Summary
## 1. Market Panorama
## 2. Key Findings (with evidence strength labels: ✅ verified / ⚠️ likely / ❌ disputed)
## 3. Competitive Landscape Matrix
## 4. Risks and Uncertainties
## 5. Conclusions and Predictions

Write to {hf(6, "final")}. Message no one.
"""
    spawn(prompt, "final-writer", wave=6, role="writer")
    launch_checkpoint(["w6_final.json"], "w6-final", "10m")
    ava.self.pause_heartbeat(600)
    log("Waiting for final report...")


# ═══════════════════════════════════════════════════════════════════════════════
# WAVE 7 — Publish: 2 review + 1 publish
# ═══════════════════════════════════════════════════════════════════════════════


def wave7():
    log("=" * 50)
    log("WAVE 7/7: Publish — two-person review + release")

    final = json.loads(ava.files.read(str(hf(6, "final"))))

    # 2 independent reviewers
    for suffix in ["a", "b"]:
        prompt = f"""You are Final Reviewer {suffix.upper()}.
Independently review the final report, looking for:
- Spelling/grammar errors
- Logical inconsistencies
- Missing important information
- Wording improvements
Return {{"approved":true/false, "changes":[...]}}
Report: {json.dumps(final, ensure_ascii=False)[:5000]}
Write to {hf(7, f"review_{suffix}")}. Message no one.
"""
        spawn(prompt, f"reviewer-{suffix}", wave=7, role=f"reviewer-{suffix}")
        log(f"  [reviewer-{suffix}] spawned")

    # 1 publisher
    publisher_prompt = f"""You are Publisher.
Read the two reviews ({hf(7, "review_a")} and {hf(7, "review_b")}),
merge revision suggestions, generate final Markdown, render it to self-contained HTML, and serve it with ava.ui.serve().
Title: "AI Coding Agent 2026 Competitive Landscape"
Message no one — publishing IS the handoff.
"""
    spawn(publisher_prompt, "publisher", wave=7, role="publisher")

    # Designated reporters: three agents run, only the two reviews gate the
    # wake-up. The publisher serves the UI itself and is never waited on.
    launch_checkpoint(["w7_review_a.json", "w7_review_b.json"], "w7-review")
    ava.self.pause_heartbeat(600)
    log("Waiting for reviews, then publisher will serve final report via UI.")


# ═══════════════════════════════════════════════════════════════════════════════
# Main — sequential wave execution
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log("🚀 Deep Research Orchestrator starting")
    log("Task: AI Coding Agent 2026 Competitive Landscape")
    log("Total: 7 waves, ~40 agents")
    log(f"Handoff: {HANDOFF}")
    log("Strategy: workers write JSON and end silently; one checkpoint per wave")

    # Wave 1 executes here. Waves 2-7 are called sequentially
    # after watcher wakes orchestrator: wave2() → wave3() → ... → wave7()
    # For demo completeness, the full call chain is shown here:
    #
    # wave1()  ← currently executing
    #   ↓ (watcher wakes orchestrator)
    # wave2()
    #   ↓
    # wave3()
    #   ↓
    # wave4()
    #   ↓
    # wave5()  ← ♻️ feedback
    #   ↓
    # wave6()
    #   ↓
    # wave7()  ← render to self-contained HTML and serve with ava.ui.serve()

    log("Wave 1 spawned. Orchestrator going idle, watcher will wake me.")
