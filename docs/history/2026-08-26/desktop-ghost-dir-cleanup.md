# desktop/ ghost directory cleanup (audit P3-2)

## What happened

The dev clone's `desktop/` directory was a ghost: it contained only an
untracked 396 MB `node_modules/` tree and no source code. The Electron
desktop-app source was moved out of the repository earlier; the directory
survived as local residue on disk.

## Audit evidence

- `git ls-files desktop/` → 0 tracked files; `git status` reports nothing for
  it (covered by the root `.gitignore` `node_modules/` rule, line 65).
- `du -sh desktop/node_modules` → 396 MB (222 packages), no hidden files, no
  running process holding a cwd or open file under `desktop/`.
- The `desktop/` path form appears in exactly one tracked file —
  `assets/agent-landscape-2026.html`, a third-party product-landscape research
  snapshot — and only as prose about other projects (`desktop/web`,
  `desktop/package.json`, `desktop/IDE`), never as a location in this repo.
  The bare word "desktop" appears widely (~100 tracked files) for the
  `ui/app` Tauri shell, desktop viewport breakpoints, and the
  computer-MCP / permissions-helper surface; none references a `desktop/`
  directory in this repo.
- The former Electron sources live on in the private history archive at the
  `archive` remote (`public-main`, commit `71ebd7d4e` "Initial public
  release"): `desktop/README.md`, `desktop.ava.okf.md`, `electron-builder.yml`,
  `electron/{auto-login,config,external-links,main}.js`, `package.json`,
  `package-lock.json`, `assets/`.

## Cleanup performed (2026-08-26)

- Removed the untracked `desktop/` directory from the dev clone
  (`rm -rf ~/Ava/desktop`, user-confirmed per repo audit 2026-08-23 P3-2).
- Nothing to change in `.gitignore` — the `node_modules/` rule already covers
  this pattern; the directory existed only as leftover local state.

## Restore path (if ever needed)

The desktop-app source is preserved in the archive remote:

    git fetch archive public-main
    git checkout archive/public-main -- desktop/

then `npm ci` inside `desktop/` and build as before. Do not expect it back in
`main` — the app's supported surface is now the permissions helper + computer
MCP, per the audit's native/desktop boundary note.
