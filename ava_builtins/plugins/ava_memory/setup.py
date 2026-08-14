"""Host-side scaffolding for the memory pool — this plugin's converge step.

Kept apart from `plugin.py` on purpose: that module imports the agent runtime
(hooks, graph, the SDK namespace), none of which exists in the `ava` CLI
process. This one depends on `shared` alone, so converge can load it without
dragging an agent into a CLI command.

What it owns is the template laid down inside the pool checkout: the index every
agent reads, and the commit hook enforcing the character caps. (The .gitignore is
not in the template — `memory_repo.init` writes it as the checkout's first
commit, since a repo needs a file to commit; one authority, not two.)

Before this step existed the template was a directory of files nothing copied,
and a pool came up holding only that .gitignore: no MEMORY.md, so the index
injection silently found nothing to inject, and no armed hook, so the caps never
fired. Prose in a skill told an agent to copy them by hand; provisioning an
invariant is not a thing to leave to prose.

Idempotent, and non-destructive: an existing file is never overwritten. The
template seeds a pool, it does not reset one — an agent's curated index must
survive every converge.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from shared.log import logger

_TEMPLATE_DIR = Path(__file__).parent / "template"

# git refuses to track an empty directory, so the template carries a .gitkeep to
# materialize `machines/`; the marker itself is not interesting to copy around.
_GITKEEP = ".gitkeep"


def _copy_template_into(pool: Path) -> list[str]:
    """Copy every template file that is missing from `pool`. Returns the
    pool-relative paths actually written, for the converge log."""
    written: list[str] = []
    for src in sorted(_TEMPLATE_DIR.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(_TEMPLATE_DIR)
        dst = pool / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # copy2 preserves the executable bit, which the commit hook needs.
        written.append(str(rel))
    return [w for w in written if not w.endswith(_GITKEEP)]


def _arm_hooks(pool: Path) -> bool:
    """Point the checkout's `core.hooksPath` at the template's `.githooks`.
    Returns True when this call changed it. Without this the pre-commit cap
    guard sits on disk unarmed — which is how it sat for the pool's whole life
    before this step."""
    current = subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if current == ".githooks":
        return False
    subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    return True


def _ensure_memory_repo() -> None:
    """Run init() if the memory pool is not a git repo. If already init'd but on the wrong branch, fail loud."""
    from shared.memory_repo import (
        MemoryBranchMismatch,
        branch_name,
        init,
        is_initialized,
    )
    from shared.paths import memory_dir

    if is_initialized():
        # init() validates branch on re-call (raises on mismatch); call it once to trigger the check
        try:
            init()
        except MemoryBranchMismatch as e:
            raise RuntimeError(str(e)) from e
        return

    branch = branch_name()
    print(f"    first-time setup: clone memory pool -> {memory_dir()} on branch {branch!r}")
    try:
        init()
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise RuntimeError(_classify_git_init_error(stderr, e.returncode)) from e
    print("    memory pool ready")


def _ensure_gateway_memory_repo() -> None:
    """Run init_gateway() to set up the gateway's consolidated memory checkout
    on `main`. Only runs when this unit carries the gateway capability.

    On a combined unit (gateway+agent-runner) this initializes the separate
    $AVA_HOME/gateway/memory checkout; on a gateway-only unit it initializes
    the same path as the agent-runner checkout (memory_dir()).
    """
    from shared.machine import is_gateway
    from shared.memory_repo import gateway_is_initialized, init_gateway
    from shared.paths import gateway_memory_dir

    if not is_gateway():
        return

    gmd = gateway_memory_dir()
    if gateway_is_initialized():
        init_gateway()  # validates branch on re-call; gateway auto-switches to main
        return

    print(f"    first-time setup: clone gateway memory pool -> {gmd} on branch 'main'")
    try:
        init_gateway()
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise RuntimeError(_classify_git_init_error(stderr, e.returncode)) from e
    print("    gateway memory pool ready")


def _classify_git_init_error(stderr: str, returncode: int) -> str:
    """Map a git-clone/push stderr blob to a single-line, actionable hint.

    Three known failure shapes get a targeted next-command; anything else falls
    through to the raw stderr so the agent still has the original text to act on.
    """
    blob = stderr.lower()
    header = f"git command failed (returncode={returncode}):\n{stderr}\n"
    if "permission denied (publickey)" in blob or "could not read from remote repository" in blob:
        return (
            f"{header}→ SSH auth to git remote failed. Verify with `ssh -T git@github.com`; "
            "if it prints `Permission denied`, add this host's `~/.ssh/id_*.pub` to your GitHub keys "
            "(Settings > SSH and GPG keys)."
        )
    if "repository not found" in blob or "does not appear to be a git repository" in blob:
        return (
            f"{header}→ Remote repo does not exist (or this host's SSH key lacks access). "
            "Create a private repo at the URL set in `AVA_MEMORY_REMOTE` (e.g. `gh repo create <user>/AvaMemory --private`), "
            "or fix the URL in `~/.ava/.env`."
        )
    if "couldn't find remote ref" in blob or "not found in upstream" in blob:
        return (
            f"{header}→ Remote branch does not exist yet. This is normal on first-ever clone — "
            "the local `init()` will push an empty branch on the next retry. If retry still fails, "
            "check that `AVA_MACHINE_NAME` matches the expected branch suffix."
        )
    return f"{header}(unrecognized failure shape — paste the stderr above into the runbook 'Secondary host bring-up' troubleshooting section)"


def scaffold() -> None:
    """Bring this cluster's memory pool up — the plugin's converge step.

    Three things, in order: the agent-runner's authoring checkout, the gateway's
    consolidated one (gateway-capable units only), and the template laid down
    inside the authoring checkout.

    All of it used to be two framework converge steps calling into the CLI. It
    is here because the pool is this plugin's, end to end: disable the plugin and
    no checkout is created, no template is laid down, and no memory note reaches
    an agent's context.
    """
    from shared.paths import memory_dir

    _ensure_memory_repo()
    _ensure_gateway_memory_repo()

    pool = memory_dir()
    if not (pool / ".git").is_dir():
        # The bring-up above declined and said why (no remote, wrong branch).
        logger.debug("[memory-scaffold] {} is not a git checkout, skipping", pool)
        return

    written = _copy_template_into(pool)
    armed = _arm_hooks(pool)
    if written:
        print(f"    seeded: {', '.join(written)}")
    if armed:
        print("    armed commit hook (core.hooksPath=.githooks)")
    logger.info("[memory-scaffold] pool={} seeded={} hook_armed={}", pool, len(written), armed)
