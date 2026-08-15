# Third-party notices

Ava itself is licensed under Apache-2.0 (see `LICENSE`). It depends on
third-party libraries that keep their own licenses; using Ava means complying
with those too. The dependency tree is overwhelmingly permissive (MIT /
Apache-2.0 / BSD / ISC) and contains **no GPL/AGPL/SSPL strong-copyleft runtime
dependency**. The libraries below carry weak-copyleft obligations worth calling
out — each is used as an **unmodified library via dynamic import** (the standard
LGPL/MPL "use without infecting" path), so the obligations attach to those
libraries' own files, not to Ava's source.

The authoritative, always-current license set is the metadata of the resolved
dependencies (`uv.lock`, `ui/web/package-lock.json`); regenerate a full
report with `uv pip licenses` / `npx license-checker` when shipping a release.

## Runtime dependencies under weak copyleft

| Package | License | Role |
|---|---|---|
| `psycopg`, `psycopg-binary`, `psycopg-pool` | LGPL-3.0-only | PostgreSQL driver |
| `pyte` | LGPL-3.0 | terminal screen model for PTY sessions (`shared/pty_sessions/screen.py`) |
| `browser-cookie3` | LGPL-3.0 | browser cookie access |
| `certifi` | MPL-2.0 | CA certificate bundle |
| `tqdm` | MPL-2.0 AND MIT | progress bars |
| `tld` (via `trafilatura`) | MPL-1.1 (elected; tri-licensed MPL-1.1 / GPL-2.0 / LGPL-2.1+) | TLD parsing for web fetch |

LGPL/MPL obligations are satisfied by using these as unmodified, separately
licensed libraries and preserving this notice. If you modify any of them, the
respective license governs that modified library's redistribution.
