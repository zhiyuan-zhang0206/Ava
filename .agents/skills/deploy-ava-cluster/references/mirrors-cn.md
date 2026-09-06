## Mirrors for restricted networks (`--mirror cn`)

On networks where `pypi.org`, `registry.npmjs.org`, and the Homebrew bottle CDN
are slow or blocked (mainland China being the motivating case), pass `--mirror cn`:

```bash
./scripts/install.sh --role gateway,agent-runner --mirror cn
```

`--mirror NAME` applies `scripts/mirrors/NAME.env` — a bundle of the index/registry
environment variables the package managers **already honor**, nothing Ava-specific.
`cn` routes:

| Leg | Mirror | Variable |
|---|---|---|
| Python locked installation | Tsinghua TUNA | `UV_DEFAULT_INDEX` |
| npm (`npm ci` at `ava start`) | npmmirror | `npm_config_registry` |
| Homebrew bottles + API (macOS) | Tsinghua TUNA | `HOMEBREW_BOTTLE_DOMAIN`, `HOMEBREW_API_DOMAIN` |

The profile is sourced for this install **and** copied to `~/.ava/mirror.env`, a
sibling of `.env` that every `ava` command loads (precedence: real env > `.env`
> `mirror.env`). That sibling is what makes the deferred `npm ci` at `ava start`
use the mirror too; hand-copying `.env.example` over `~/.ava/.env` never touches it.

The shared `uv.lock` uses PyPI and `files.pythonhosted.org`, including on
GitHub-hosted CI runners. Do not commit a host's mirror-resolved lock. The
`lint-python-lock` pre-commit hook and CI's dependency-free source check enforce
that boundary; a CI environment index override cannot replace artifact URLs
already embedded in a frozen lock.

Installation and updates use the same dependency-free `cli.python_install`
entry point. It checks the canonical lock, exports exact requirements and hashes
with offline `uv export --locked`, and installs those artifacts through the
host's index using `uv pip install --no-deps --require-hashes`. The real checkout
is then installed editable with uv's normal build isolation. No lock is rewritten
or re-resolved for a mirror. Existing machine uv and pip single-index settings
are also recognized; explicit environment settings take precedence. See
[Machine Python indexes](../../../../conventions/dev-setup.md#machine-python-indexes)
for configuration precedence and supported transport settings.

A process still running an older updater can execute its old mirror-incompatible
prepare command before loading this helper. First rollout must verify the
executing updater version; repository merge alone does not establish that
bootstrap path. The shared helper's local tests do not replace that production
check.

On macOS, selecting the mirror also installs `uv` via Homebrew, avoiding the
pinned uv release's GitHub binary download. Homebrew is already mirror-routed.

**Residual legs with no canonical CN mirror** (set these yourself if they stall):

- uv's managed CPython (`uv python install 3.12`) is one ~30 MB GitHub-release
  download. If it hangs, pre-install Python 3.12 (e.g. `brew install python@3.12`)
  or set `UV_PYTHON_INSTALL_MIRROR` to a GitHub proxy you trust.
- On **Linux**, the `uv` binary (`UV_RELEASE_BASE_URL` — toolchain.sh's download
  base, still sha256-verified) and the apt repos for Node/PGDG/GitHub-CLI have no
  clean CN mirror — point your apt sources and those vars at a proxy you trust.
  macOS is the clean end-to-end path.

`--mirror` is refused together with `--worktree` because that flag writes a unit
profile and selects host-global download routes. Worktree Python installation
still honors existing machine index settings through the same locked helper.
