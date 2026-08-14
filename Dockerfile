# Ava unified container image — shared by CI / eval / future local multi-machine sim.
#
# Build: `docker build -t ghcr.io/<owner>/ava:latest .` (on a
# self-hosted Linux runner; `.github/workflows/build-image.yml` auto-rebuilds +
# pushes to GHCR whenever Dockerfile changes).
#
# Design philosophy:
# - A single image covers all use cases (test / eval / multi-node), eliminating
#   the "every job uses a different image and each is missing some apt package X"
#   patch-on-patch loop
# - Conceptually = one Ava machine, 1:1 mirror of prod (a single box running the full stack)
# - Higher layers more stable, lower layers more volatile — touching the end of
#   Dockerfile rebuilds in 1-2min, touching the top forces a full rebuild
#
# When upgrading the playwright version, you MUST sync (the image bundles a
# chromium binary that's tightly coupled to the playwright code; mismatched
# versions yield "Executable doesn't exist at /ms-playwright/
# chromium_headless_shell-..."):
#   - playwright dep in `pyproject.toml`
#   - `PLAYWRIGHT_VERSION` env in this file
#   - Trigger build-image.yml to rebuild + push (workflow_dispatch / push Dockerfile)
#
# pyproject.toml / uv.lock changes also trigger the trailing `uv sync` layer to
# re-run (image-self-contained deps, see the layer header), ~1-2 min rebuild.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_VERSION=1.59.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
# uv installs to ~/.local/bin by default; add it to PATH
ENV PATH="/root/.local/bin:${PATH}"

# ── Layer 1: system base deps (not in base image / only used by this image) ──────────
# Bundle locale-gen with apt-get install to avoid bouncing between layers. BuildKit
# `cache, sharing=locked` makes apt cache persistent across builds, no re-fetching debs.
# Don't install git/curl/CLI tools here — those go through the shared script in Layer 2.
# postgresql-17 isn't in ubuntu noble's default repos (it ships pg-16), so add the
# PostgreSQL official PGDG apt repo first (needs ca-certificates/curl/gnupg). pg-17
# is required: tests/_containers.py runs initdb to spawn native per-worker clusters.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev python3-pip \
        postgresql-17 redis-server \
        build-essential locales \
    && locale-gen en_US.UTF-8

# ── Layer 2: CLI tools (shared with Dockerfile.eval-bench via install-cli-tools.sh) ──
# git / ripgrep / jq / fd-find / bat / tree / fzf / tmux / htop / gh
# + GitHub CLI keyring. To change the CLI list, only edit install-cli-tools.sh.
COPY scripts/install-cli-tools.sh /tmp/install-cli-tools.sh
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    bash /tmp/install-cli-tools.sh && rm /tmp/install-cli-tools.sh

# ── Layer 3: node 22 (provision/node.sh — shared with install.sh / CI) ────────────────
# Ubuntu 24.04 defaults to node 18; the script upgrades to 22 via nodesource.
COPY scripts/provision/_lib.sh scripts/provision/node.sh /tmp/provision/
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    bash /tmp/provision/node.sh

# ── Layer 4: uv (provision/toolchain.sh — shared) ────────────────────────────
# Installs only the uv binary (to /root/.local/bin, already on PATH). The venv is
# baked in a dedicated layer below.
COPY scripts/provision/_lib.sh scripts/provision/toolchain.sh /tmp/provision/
RUN bash /tmp/provision/toolchain.sh

# ── Layer 5: playwright + chromium (provision/install-playwright.sh — shared) ──────────
# Pinned via the PLAYWRIGHT_VERSION env above (single source with pyproject); the
# script installs the playwright CLI to system python (`--break-system-packages`)
# and the chromium build to PLAYWRIGHT_BROWSERS_PATH, which the uv-venv playwright
# reuses.
COPY scripts/provision/install-playwright.sh /tmp/provision/
RUN bash /tmp/provision/install-playwright.sh

# ── Project Python deps preinstalled to /opt/ava-venv ────────────────────────────
# Image-self-contained: deps are installed into the image rather than runtime sync. Use cases:
# - CI: with UV_PROJECT_ENVIRONMENT set, the job step's `uv sync --frozen` is a
#   noop when lock matches, saving ~10s
# - Eval (driver_container.py): code is bind-mounted to /ava (ro); venv must live
#   inside the image (host venv binaries aren't macOS/Linux compatible)
# - Future multi-node sim: `docker run` can immediately `python -m agent`, no sync
#
# Only COPY pyproject.toml + uv.lock (not the full code) — only changes to these
# two files invalidate the layer cache; image rebuilds on .py source changes don't
# re-sync deps.
ENV UV_PROJECT_ENVIRONMENT=/opt/ava-venv
ENV VIRTUAL_ENV=/opt/ava-venv
ENV PATH="/opt/ava-venv/bin:${PATH}"
COPY pyproject.toml uv.lock /tmp/ava-deps/
# The optional corp_ca BuildKit secret: on the corp runner the egress to
# pypi.org is TLS-intercepted, so this fetch must also trust the corp CA
# (build-image.yml passes just that cert via --secret; BuildKit caps secrets at
# 500KiB so a full merged bundle doesn't fit — merge with the system roots
# here instead). A secret mount never lands in an image layer — the published
# image's trust store stays stock. Absent secret (clean networks) = plain
# uv sync, unchanged.
RUN --mount=type=secret,id=corp_ca \
    cd /tmp/ava-deps && \
    if [ -f /run/secrets/corp_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/corp_ca > /tmp/ca-bundle.pem && \
        export SSL_CERT_FILE=/tmp/ca-bundle.pem; \
    fi && \
    uv sync --frozen && rm -rf /tmp/ava-deps /tmp/ca-bundle.pem

WORKDIR /workspace
CMD ["/bin/bash"]
