#!/usr/bin/env bash
# Ava install script — OS-aware, capability-scoped entry across macOS / Linux (incl. WSL / containers) / Windows (WSL2).
#
# Capabilities (a host carries one or both):
#   gateway        macOS: brew pg17 + redis@8.2 (native); Linux: apt pg17 + redis.
#   agent-runner   no local pg/redis (a runner-only host connects to a gateway's instance).
#
# Linux provision (CLI tools / node / pg17 / redis / pgbouncer) presence-checks
# its packages and skips apt on a host that already has them — a WSL distro
# without sudo can install; `ava start` provisions the per-cluster Postgres
# under $AVA_HOME/pg itself (no system data dir to bootstrap). See
# conventions/windows-setup.md.
#
# All paths share: uv + Python 3.12, locked Python installation, ~/.local/bin/ava symlink.
#
# --role is REQUIRED — a comma-separated capability set, no default. A single box
# carries BOTH on one unit (owns the data plane AND runs agents); split
# deployments give each capability its own machine. See .agents/skills/deploy-ava-cluster/SKILL.md.
#
# Usage:
#     ./scripts/install.sh --role gateway,agent-runner   # single box (most installs)
#     ./scripts/install.sh --role gateway                # gateway-only host (pg/redis + gateway)
#     ./scripts/install.sh --role agent-runner           # runner-only host (no local pg/redis)
#     ./scripts/install.sh --role observability-station  # LGTM observability backends host
#     ./scripts/install.sh --role gateway,agent-runner --mirror cn # route pip/npm/brew through CN mirrors
#     ./scripts/install.sh --worktree [--path P] [--no-seed]       # dev worktree cluster (see below)
#     # For auth on: read/export AVA_INSTALL_CLUSTER_SECRET without echo first, then run:
#     ./scripts/install.sh --role gateway,agent-runner
#
# The final step of every install is cluster birth (`python -m cli.install_cluster`):
# a gateway-capable role gets its registry record + its own pg/redis instance +
# provisioned database + `$AVA_HOME/.env`. The cluster secret follows the role:
# a single-machine role (gateway,agent-runner) births a NO-AUTH cluster (empty
# secret — every surface serves unauthenticated on loopback) unless the one-shot
# AVA_INSTALL_CLUSTER_SECRET states one; a gateway-only split host mints a fresh secret
# (remote agent-runners depend on it); a secret already in the .env is never
# rotated. The --role capability set is written into `.env` as the serve flags.
# Idempotent — an already-installed home is a no-op. An agent-runner-only role
# does not birth (its identity arrives via `ava enroll`). `ava start` stays the
# only bring-up.
#
# --worktree births a dev worktree's own cluster and skips every host-global step
# (brew/apt, the install-dir guard, the ~/.local/bin symlink; --mirror is
# refused). Identity is the path: home defaults to ~/.ava-<checkout-dir>
# (derived from this script's checkout, never the cwd) and --path is the only
# override — there is no name flag. Runs the locked Python installer, births the cluster
# (single-machine -> NO-AUTH, empty secret unless AVA_INSTALL_CLUSTER_SECRET states one;
# never inherited from prod), writes the checkout's `.ava_home`
# pointer, and seeds the SEED_ENV_KEYS allowlist (LLM + web-search keys) from
# ~/.ava/.env (--no-seed skips). Start it with the worktree's own `.venv/bin/ava start`.
#
# --mirror NAME (optional) applies scripts/mirrors/NAME.env — a bundle of the
# index/registry env vars the package managers already honor (PyPI / npm /
# Homebrew). It is sourced for this run AND copied to ~/.ava/mirror.env, which
# every `ava` command loads, so the frontend's `npm ci` at `ava start` resolves
# from the mirror too. `cn` (mainland China) is the only profile today.
#
# Install-dir guard: must run from $AVA_HOME/source/ (default $HOME/.ava/source/). Override
# via $AVA_HOME env var for non-default homes (e.g. AVA_HOME=$HOME/.ava_gateway ./scripts/install.sh).
# On Windows this runs inside WSL2 — see conventions/windows-setup.md.

set -euo pipefail

# --- argument parsing ---------------------------------------------------------
# No silent role default: a fresh-host operator must state the role explicitly,
# so the install never quietly picks "gateway" for someone who meant to
# add an agent-runner.
ROLE=""
MIRROR=""
WORKTREE=0
WT_PATH=""
SEED=1
# Capture the dedicated one-shot input into a non-exported shell variable, then
# remove it before uv/package-manager children run. Only the final cluster-birth
# Python child receives it. The compatibility argv flag below can override it.
CLUSTER_SECRET="${AVA_INSTALL_CLUSTER_SECRET-}"
# An inherited variable with this implementation-detail name would otherwise
# retain Bash's export attribute after assignment.
export -n CLUSTER_SECRET
unset AVA_INSTALL_CLUSTER_SECRET

while [ $# -gt 0 ]; do
    case "$1" in
        --cluster-secret=*) CLUSTER_SECRET="${1#--cluster-secret=}"; shift ;;
        --cluster-secret)
            [ $# -ge 2 ] || { echo "install.sh: --cluster-secret requires an argument — the cluster's AVA_CLUSTER_SECRET (URL-safe token), or omit it for the role default (single box: no auth; gateway-only split: minted)" >&2; exit 2; }
            CLUSTER_SECRET="$2"; shift 2 ;;
        --role=*) ROLE="${1#--role=}"; shift ;;
        --role)
            [ $# -ge 2 ] || { echo "install.sh: --role requires an argument — a capability set, e.g. gateway,agent-runner | gateway | agent-runner" >&2; exit 2; }
            ROLE="$2"; shift 2 ;;
        --mirror=*) MIRROR="${1#--mirror=}"; shift ;;
        --mirror)
            [ $# -ge 2 ] || { echo "install.sh: --mirror requires an argument — a profile name, e.g. cn" >&2; exit 2; }
            MIRROR="$2"; shift 2 ;;
        --worktree) WORKTREE=1; shift ;;
        --path=*) WT_PATH="${1#--path=}"; shift ;;
        --path)
            [ $# -ge 2 ] || { echo "install.sh: --path requires an argument — the worktree cluster home, e.g. ~/.ava-mytask" >&2; exit 2; }
            WT_PATH="$2"; shift 2 ;;
        --no-seed) SEED=0; shift ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
OS="$(uname)"
# `prov_sudo` lives in provision/_lib.sh (single definition shared with the
# provision pieces). A stubbed checkout that carries only this script (the
# install.sh contract tests) gets the same helper inline.
if [ -f "$SCRIPT_DIR/provision/_lib.sh" ]; then
    . "$SCRIPT_DIR/provision/_lib.sh"
else
    prov_sudo() {
        if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
            sudo "$@"
        else
            "$@"
        fi
    }
fi

die() { echo "install.sh: $*" >&2; exit 2; }

# --- flag cross-validation (before any side effect, safe from any cwd) ---------
if [ "$WORKTREE" = 1 ]; then
    # A worktree cluster is always the single-box gateway,agent-runner shape, and
    # host-global steps (where a mirror matters) are skipped entirely.
    [ -z "$ROLE" ] || die "--worktree and --role are mutually exclusive (a worktree cluster is always gateway,agent-runner)"
    [ -z "$MIRROR" ] || die "--worktree does not take --mirror (use existing machine index settings; --mirror writes a unit profile)"
else
    [ -z "$WT_PATH" ] || die "--path requires --worktree"
    [ "$SEED" = 1 ] || die "--no-seed requires --worktree"

    # --role is a comma-separated capability set: gateway, agent-runner, or both.
    # Validate every token; an empty set or an unknown token fails loud.
    role_valid=1
    [ -n "$ROLE" ] || role_valid=0
    _old_ifs="$IFS"; IFS=','
    for tok in $ROLE; do
        case "$tok" in
            gateway|agent-runner) ;;
            *) role_valid=0 ;;
        esac
    done
    IFS="$_old_ifs"
    if [ "$role_valid" != 1 ]; then
        cat >&2 <<EOF
install.sh: --role is required — a comma-separated capability set, no default (got: '${ROLE}').

  gateway        owns Postgres/Redis + the HTTP gateway.
  agent-runner   runs agents; a runner-only host connects to a gateway by URL.

A single box carries BOTH on one unit (owns the data plane AND runs agents).
Split deployments give each capability its own machine. Walkthrough:
.agents/skills/deploy-ava-cluster/SKILL.md.

  ./scripts/install.sh --role gateway,agent-runner   # single box (most installs)
  ./scripts/install.sh --role gateway                # gateway-only host
  ./scripts/install.sh --role agent-runner           # runner-only host

Dev worktree clusters use --worktree instead (no --role):
  ./scripts/install.sh --worktree [--path ~/.ava-<name>]
EOF
        exit 2
    fi
fi

# --- non-root guard (Linux) ----------------------------------------------------
# The install's final birth step runs initdb, and Postgres refuses to run
# initdb as root — a fresh Linux VPS usually lands you as root, and the failure
# would come only after the whole toolchain install. Refuse up front when the
# role implies a birth. Runner-only hosts never birth (identity arrives via
# `ava enroll`), so they stay allowed as root. The apt steps below use
# passwordless sudo automatically (`prov_sudo`) when run as a non-root user.
if [ "$OS" = "Linux" ] && [ "$(id -u)" = 0 ] && [ "${AVA_ALLOW_ROOT_INSTALL:-0}" != 1 ]; then
    _needs_birth=0
    if [ "$WORKTREE" = 1 ]; then
        _needs_birth=1
    else
        case "$ROLE" in
            *gateway*) _needs_birth=1 ;;
        esac
    fi
    if [ "$_needs_birth" = 1 ]; then
        cat >&2 <<'EOF'
install.sh: refusing to run as root on Linux — the install births a per-cluster
Postgres via initdb, and Postgres refuses to run initdb as root, so the birth
would fail after the full toolchain install.

  # create a dedicated user with passwordless sudo (the apt steps need it), then re-run
  adduser ava && echo 'ava ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ava
  su - ava
  mkdir -p ~/.ava && cd ~/.ava
  git clone https://github.com/zhiyuan-zhang0206/Ava.git source && cd source
  ./scripts/install.sh --role gateway,agent-runner

Runner-only hosts (--role agent-runner, no local data plane) may install as root.
Containers that pre-provision the pg template may set AVA_ALLOW_ROOT_INSTALL=1.
EOF
        exit 2
    fi
fi

# --- enforce canonical prod install path --------------------------------------
# Prod runtime expects the source tree at $AVA_HOME/source so:
# - `ava start` infers the correct session cwd.
# - the .venv embedded path matches where `ava` was symlinked from.
#
# Worktree dev clones use the --worktree mode, which skips this guard (their
# checkout deliberately lives outside $AVA_HOME/source), so this gate only
# fires for the fresh-host bootstrap path (which is exactly when getting
# the path wrong is hardest to undo without reinstalling).
#
# Override: set AVA_HOME before running to install under a non-default home
# (e.g. AVA_HOME=$HOME/.ava_gateway ./scripts/install.sh --role gateway).
_AVA_HOME="${AVA_HOME:-$HOME/.ava}"
EXPECTED_INSTALL_DIR="$_AVA_HOME/source"
ACTUAL_DIR="$(pwd -P)"
if [ "$WORKTREE" != 1 ] && [ "$ACTUAL_DIR" != "$EXPECTED_INSTALL_DIR" ]; then
    cat >&2 <<EOF
install.sh: must run from \$AVA_HOME/source/ (current cwd: $ACTUAL_DIR)

Why: prod conventions (session cwd, the path
embedded in .venv/bin/ava) all assume the source tree lives at
\$AVA_HOME/source/. Installing elsewhere quietly creates a parallel dev
volume and the agents you spawn afterwards run against a different DB
than your existing prod data.

First-time install on a fresh host:
  mkdir -p $_AVA_HOME
  cd $_AVA_HOME
  git clone https://github.com/zhiyuan-zhang0206/Ava.git source
  cd source
  bash scripts/install.sh${ROLE:+ --role $ROLE}

If you already cloned elsewhere and want to move it:
  mv "$ACTUAL_DIR" $_AVA_HOME/source
  cd $_AVA_HOME/source
  bash scripts/install.sh${ROLE:+ --role $ROLE}

To install under a non-default home, set AVA_HOME first:
  AVA_HOME=\$HOME/.ava_gateway bash scripts/install.sh --role gateway
EOF
    exit 2
fi

# ===========================================================================
# warn_node_macos: the gateway runs the Next.js frontend, which needs Node >=
# 20.9. The Linux gateway path installs node 22 via nodesource; macOS cannot
# install silently. Warn (don't block) when node is missing or too old — a
# gateway can run headless without the frontend (`ava start --disable-service
# frontend`), so this is a heads-up, not a hard requirement.
# ===========================================================================
warn_node_macos() {
    if command -v node >/dev/null 2>&1; then
        major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
        [ "$major" -ge 20 ] 2>/dev/null && return 0
        echo "install.sh: WARNING node $(node --version) is older than the frontend's Node >= 20.9." >&2
    else
        echo "install.sh: WARNING node is not on PATH; the gateway's Next.js frontend needs Node >= 20.9." >&2
    fi
    echo "  Run \`brew install node\` for the web UI, or run the gateway headless (\`ava start --disable-service frontend\`)." >&2
}

# warn_browser_deps: the ava-browser service runs chrome-devtools-mcp through
# npx, so Node.js is a hard install-time dependency. After node provisioning,
# re-check npx and warn loudly with the fix — an install must never leave a
# host whose ava-browser silently skips forever (company-mini, 2026-08-27).
warn_browser_deps() {
    command -v npx >/dev/null 2>&1 && return 0
    echo "install.sh: WARNING ava-browser needs Node.js (npx); it is not on PATH — ava-browser will stay skipped on this host." >&2
    case "$OS" in
        Darwin) echo "  Run \`brew install node\` (or \`brew install node@22 && brew link --force node@22\`) then re-run this install." >&2 ;;
        Linux)  echo "  Run \`curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs\` then re-run this install." >&2 ;;
    esac
}

# ===========================================================================
# common_host_wiring: shared by both roles.
#   - installs uv + Python 3.12 if missing
#   - installs the canonical lock through the configured machine index
#   - symlinks .venv/bin/ava into ~/.local/bin for the prod cluster only
#   - ensures ~/.local/bin is on PATH for the rest of this script
# ===========================================================================
link_bare_ava() {
    local bare_link="$HOME/.local/bin/ava"
    local checkout_ava="$PWD/.venv/bin/ava"
    local prod_home="${HOME}/.ava"
    local install_home="${_AVA_HOME%/}"
    while [[ "$install_home" == */ && "$install_home" != "/" ]]; do
        install_home="${install_home%/}"
    done
    while [[ "$prod_home" == */ && "$prod_home" != "/" ]]; do
        prod_home="${prod_home%/}"
    done

    if [ "$install_home" = "$prod_home" ]; then
        # -n prevents ln from following a symlink whose target is a directory.
        ln -sfn "$checkout_ava" "$bare_link"
        return 0
    fi

    if [ -L "$bare_link" ]; then
        local current_target
        current_target="$(readlink "$bare_link")"
        [ "$current_target" = "$checkout_ava" ] && return 0
        echo "install.sh: WARNING non-prod install left $bare_link pointing at '$current_target'." >&2
    elif [ -e "$bare_link" ]; then
        echo "install.sh: WARNING non-prod install left existing $bare_link untouched (not a symlink)." >&2
    else
        echo "install.sh: WARNING non-prod install did not create $bare_link (no symlink exists)." >&2
    fi
    echo "  Re-link prod with: ln -sfn \"$HOME/.ava/source/.venv/bin/ava\" \"$HOME/.local/bin/ava\"" >&2
}

common_host_wiring() {
    # uv via the shared toolchain piece (idempotent — skips if uv is present). The
    # pinned uv release download needs curl, which the gateway path already has but a
    # runner-only Linux host may not, so ensure it first.
    if [ "$OS" != "Darwin" ] && ! command -v curl >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        prov_sudo apt-get update && prov_sudo apt-get install -y --no-install-recommends curl ca-certificates
    fi
    # Under a mirror on macOS, install uv via Homebrew (already routed through the
    # bottle mirror) so the pinned uv release's GitHub download is skipped;
    # toolchain.sh then sees uv present and no-ops.
    if [ -n "$MIRROR" ] && [ "$OS" = "Darwin" ] && ! command -v uv >/dev/null 2>&1; then
        brew install uv
    fi
    bash "$SCRIPT_DIR/provision/toolchain.sh"
    export PATH="$HOME/.local/bin:$PATH"

    # pyproject.toml requires-python = ">=3.12"; fetch a managed Python if absent.
    uv python install 3.12

    # The dependency-free installer validates the canonical lock first, then
    # uses a machine uv/pip index only as transport. Never rewrite runtime pins
    # or remove existing dev packages merely because this host uses a mirror.
    env -u VIRTUAL_ENV uv run --no-project --python 3.12 python "$SCRIPT_DIR/../cli/python_install.py" --locked --inexact --mirror-env "$_AVA_HOME/mirror.env"

    # Bootstrap `ava` onto PATH: the editable install generates .venv/bin/ava but a
    # project venv is not on PATH by design. Symlink into ~/.local/bin so
    # `ava` is callable right after install.
    mkdir -p "$HOME/.local/bin"
    link_bare_ava
    export PATH="$HOME/.local/bin:$PATH"
}

# ===========================================================================
# gateway path: owns the data plane (pg/redis).
#   macOS  — brew installs pg17 + redis@8.2 (native, no docker); brew initdb's pg.
#   Linux  — apt installs pg17 + redis-server; one-time initdb bootstrap.
# ===========================================================================
install_gateway() {
    case "$OS" in
        Darwin)
            # macOS: native pg/redis@8.2 via Homebrew (no docker). `brew install`
            # runs initdb for postgresql@17; `ava start`
            # (cli/commands/_cluster_instance.py) creates the `ava` role + db
            # on first boot and runs the per-cluster data plane under $AVA_HOME.
            bash "$SCRIPT_DIR/provision/database.sh"
            # The gateway runs the frontend and ava-browser needs npx; attempt
            # the shared idempotent provisioner before its existing UI warning.
            if ! bash "$SCRIPT_DIR/provision/node.sh"; then
                echo "install.sh: WARNING Node.js provisioning failed; see the next warning for manual repair." >&2
            fi
            warn_node_macos
            ;;
        Linux)
            export DEBIAN_FRONTEND=noninteractive
            # CLI tools (gh / git / rg / jq / fd ...).
            bash "$SCRIPT_DIR/install-cli-tools.sh"
            # node 22 — the frontend (Next.js) needs >= 20.9; apt default is 18.
            bash "$SCRIPT_DIR/provision/node.sh"
            # pg17 + redis servers (ava start drives the per-cluster instance via
            # pg_ctl + redis-server, not docker).
            bash "$SCRIPT_DIR/provision/database.sh"
            ;;
        *)
            die "unsupported OS: $OS (supported: Darwin / Linux)"
            ;;
    esac

    common_host_wiring
}

# ===========================================================================
# agent-runner path: NO local pg/redis (connects to gateway's instance).
#   Both macOS and Linux provision Node.js for ava-browser, then install uv +
#   Python + sync. There is no local pg/redis on this role.
# ===========================================================================
install_agent_runner() {
    case "$OS" in
        Darwin|Linux) ;;
        *) die "unsupported OS: $OS (supported: Darwin / Linux)" ;;
    esac

    # The runner's shared headed browser (ava-browser) needs Node.js for its
    # chrome-devtools-mcp upstream — provision it at install time instead of
    # leaving the service silently skipped at every start (company-mini
    # 2026-08-27: enrolled runner, no npx, browser skipped since).
    if ! bash "$SCRIPT_DIR/provision/node.sh"; then
        echo "install.sh: WARNING Node.js provisioning failed; see the next warning for manual repair." >&2
    fi
    warn_browser_deps

    common_host_wiring
}

# ===========================================================================
# birth_cluster: install-time cluster birth — the final step of a prod install.
#   Thin Python entry (cli.install_cluster) reusing the birth primitives:
#   a gateway-capable role gets registry record + its own pg/redis instance +
#   provisioned db + $AVA_HOME/.env (cluster secret minted when absent);
#   an agent-runner-only role writes just the serve flags (identity arrives via
#   `ava enroll`). Idempotent — an already-installed home is a no-op. Never
#   starts services; `ava start` stays the only bring-up.
# ===========================================================================
birth_cluster() {
    birth_args=("--home" "$_AVA_HOME" "--role" "$ROLE")
    if [ -n "$CLUSTER_SECRET" ]; then
        (cd "$EXPECTED_INSTALL_DIR" && AVA_INSTALL_CLUSTER_SECRET="$CLUSTER_SECRET" .venv/bin/python -m cli.install_cluster "${birth_args[@]}")
    else
        (cd "$EXPECTED_INSTALL_DIR" && .venv/bin/python -m cli.install_cluster "${birth_args[@]}")
    fi
}

# ===========================================================================
# print_next_steps: role-aware operator guidance, printed AFTER birth so the
# messages appear in execution order.
# ===========================================================================
print_next_steps() {
    echo ""
    case "$ROLE" in
        *gateway*)
            if [ -n "$CLUSTER_SECRET" ]; then
                secret_line="the cluster secret you supplied for this install in $_AVA_HOME/.env"
            elif [ "$ROLE" = "gateway" ]; then
                secret_line="a minted cluster secret in $_AVA_HOME/.env (split deployment — remote runners need it)"
            else
                secret_line="NO cluster secret (single-box no-auth: gateway API / /ops / pg / redis all serve unauthenticated on loopback; set AVA_INSTALL_CLUSTER_SECRET for the install to turn auth on)"
            fi
            echo "gateway install complete — cluster born under $_AVA_HOME (its own pg/redis,"
            echo "provisioned db, derived urls, $secret_line,"
            echo "serve flags from --role)."
            echo "Next: add AVA_MODEL + its provider key to $_AVA_HOME/.env (see .env.example"
            echo "for the template — do not copy it wholesale over the derived values), then start:"
            echo "  ava start --machine-name <name> --gateway-url <url>"
            ;;
        *)
            echo "agent-runner install complete."
            echo "Next: enroll this machine with the gateway, then start it:"
            echo "  read AVA_CLUSTER_SECRET without echo, export it, then run:"
            echo "  ava enroll --gateway <url> --machine-name <name> --machine-host <this-host-addr>"
            echo "  ava start"
            echo "(get <url> + <secret> from the gateway operator; enroll presents the secret to the gateway's authenticated /api/bootstrap, which returns this host's config)"
            ;;
    esac
}

# ===========================================================================
# worktree mode: birth a dev worktree's own cluster — no host-global steps.
#   Skips brew/apt, the install-dir guard, the ~/.local/bin symlink. Does:
#   locked Python install + cluster birth (registry + its own pg/redis + .env — a
#   single-machine birth, so NO-AUTH with an empty secret by default, or the
#   AVA_INSTALL_CLUSTER_SECRET the caller states; never inherited from prod) + the
#   checkout's .ava_home pointer + convenience-key seeding from ~/.ava/.env
#   (--no-seed to skip). The checkout is SCRIPT_DIR/.. — never the cwd (a
#   worktree shell's cwd can be reset elsewhere). Identity is the path: home
#   defaults to ~/.ava-<checkout-dir> and --path is the only identity input (the
#   transitional internal name derives from the home basename); a default
#   already claimed by another checkout is refused (pass --path).
# ===========================================================================
install_worktree() {
    checkout_dir="$(dirname "$SCRIPT_DIR")"
    checkout_name="$(basename "$checkout_dir")"
    # Identity is the path: the default home follows the ~/.ava-<name> convention
    # (name = checkout dir), so a later `ava start` derives the SAME identity from
    # the home. --path is the only identity input.
    target_home="${WT_PATH:-$HOME/.ava-$checkout_name}"
    command -v uv >/dev/null 2>&1 || die "--worktree needs uv on PATH (dev-host prerequisite; see conventions/dev-setup.md)"
    command -v python3 >/dev/null 2>&1 || die "--worktree needs python3 to run the editable-venv guard"
    (cd "$checkout_dir" && python3 "$SCRIPT_DIR/guard_editable_venv.py" "$checkout_dir")
    (cd "$checkout_dir" && env -u VIRTUAL_ENV uv run --no-project --python 3.12 python "$checkout_dir/cli/python_install.py" --locked --inexact --mirror-env "$target_home/mirror.env")
    wt_args=("--home" "$target_home" "--role" "gateway,agent-runner" "--worktree")
    # A worktree birth is single-machine -> NO-AUTH (empty secret) by default;
    # AVA_INSTALL_CLUSTER_SECRET states one explicitly. The seed source is stated
    # explicitly too (no hidden default): the prod home's .env.
    if [ "$SEED" = 1 ]; then wt_args+=("--seed" "--seed-source" "$HOME/.ava/.env"); fi
    if [ -n "$CLUSTER_SECRET" ]; then
        (cd "$checkout_dir" && AVA_INSTALL_CLUSTER_SECRET="$CLUSTER_SECRET" .venv/bin/python -m cli.install_cluster "${wt_args[@]}")
    else
        (cd "$checkout_dir" && .venv/bin/python -m cli.install_cluster "${wt_args[@]}")
    fi
    echo ""
    echo "worktree cluster installed (home: $target_home)."
    echo "Start it with this checkout's own CLI:"
    # %q so the printed command stays copy-pasteable when the checkout path has spaces.
    printf '  %q start\n' "$checkout_dir/.venv/bin/ava"
}

# ===========================================================================
# apply_mirror: when --mirror NAME is given, source scripts/mirrors/NAME.env so
# this run's uv / brew steps use the mirrors, and copy it to ~/.ava/mirror.env
# so every later `ava` command (notably `npm ci` at `ava start`) inherits the
# same registry env. No-op without --mirror. Runs before any download step.
# ===========================================================================
apply_mirror() {
    [ -n "$MIRROR" ] || return 0
    profile="$SCRIPT_DIR/mirrors/$MIRROR.env"
    if [ ! -f "$profile" ]; then
        avail="$(cd "$SCRIPT_DIR/mirrors" 2>/dev/null && ls *.env 2>/dev/null | sed 's/\.env$//' | tr '\n' ' ')"
        die "unknown --mirror '$MIRROR' (no $profile). Available: ${avail:-none}"
    fi
    echo "install.sh: applying mirror profile '$MIRROR' ($profile)"
    set -a; . "$profile"; set +a
    mkdir -p "$_AVA_HOME"
    cp "$profile" "$_AVA_HOME/mirror.env"
    echo "  · wrote $_AVA_HOME/mirror.env (loaded by every ava command)"
}

# --- dispatch by capability ---------------------------------------------------
# A role containing `gateway` (incl. the single-box `gateway,agent-runner`) sets
# up the data plane (pg/redis) + host wiring; agent-runner adds no host setup
# beyond the shared wiring, so a runner-only role takes the lighter path. Every
# path ends in the birth step; `ava start` later brings up the union of services
# the capability set needs. --worktree replaces the whole host path with the
# worktree cluster flow.
apply_mirror
if [ "$WORKTREE" = 1 ]; then
    install_worktree
else
    case "$ROLE" in
        *gateway*)    install_gateway ;;
        *)            install_agent_runner ;;
    esac
    birth_cluster
    print_next_steps
fi

printf '\n  PATH note: if `ava` is not found in a new shell, add it:\n  export PATH="$HOME/.local/bin:$PATH"\n'
