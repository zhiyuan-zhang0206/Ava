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
| PyPI (`uv sync`) | Tsinghua TUNA | `UV_DEFAULT_INDEX` |
| npm (`npm ci` at `ava start`) | npmmirror | `npm_config_registry` |
| Homebrew bottles + API (macOS) | Tsinghua TUNA | `HOMEBREW_BOTTLE_DOMAIN`, `HOMEBREW_API_DOMAIN` |

The profile is sourced for this install **and** copied to `~/.ava/mirror.env`, a
sibling of `.env` that every `ava` command loads (precedence: real env > `.env`
> `mirror.env`). That sibling is what makes the deferred `npm ci` at `ava start`
use the mirror too; hand-copying `.env.example` over `~/.ava/.env` never touches it.

Two implementation details worth knowing:

- **`uv sync` re-resolves under a mirror** (the flag drops `--frozen`). The
  committed `uv.lock` pins `files.pythonhosted.org` wheel URLs that `--frozen`
  would fetch verbatim, ignoring the index override; re-resolving against
  `UV_DEFAULT_INDEX` rewrites the *local* lock to mirror URLs (uncommitted). A
  full mirror resolves the same versions.
- **macOS installs `uv` via Homebrew** under a mirror, so the astral installer's
  GitHub-release download is skipped (Homebrew is already mirror-routed).

**Residual legs with no canonical CN mirror** (set these yourself if they stall):

- uv's managed CPython (`uv python install 3.12`) is one ~30 MB GitHub-release
  download. If it hangs, pre-install Python 3.12 (e.g. `brew install python@3.12`)
  or set `UV_PYTHON_INSTALL_MIRROR` to a GitHub proxy you trust.
- On **Linux**, the `uv` binary (`UV_INSTALLER_GITHUB_BASE_URL`) and the apt repos
  for Node/PGDG/GitHub-CLI have no clean CN mirror — point your apt sources and
  those vars at a proxy you trust. macOS is the clean end-to-end path.

`--mirror` is refused together with `--worktree`: a worktree cluster runs no
host-global download step (`uv sync --frozen` follows the committed lock), so
there is nothing for a mirror to route.
