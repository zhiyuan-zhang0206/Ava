"""Time-boxed release-probe shell left by the `shared` -> `base` package rename.

Nothing in this repository imports `shared`: the package is `base`. This shell
exists only because release preparation crosses versions. The preparing release
runs `base.deploy.release.runtime_prepare.ABI_PROBE` and `PLUGIN_PROBE` inside
the TARGET image's own interpreter (an upgrade prepares a newer image, a
rollback an older one), and both probes name the pre-rename package:
`shared.runtime_abi.current_abi` and
`shared.runtime_plugins.verify_plugin_dependencies`. The builder also writes the
identity member `shared/release-build.json`
(`base.deploy.release.identity.IDENTITY_MEMBER`) into the target commit's source
tree, so this directory must exist in every image.

It holds exactly three files - this one, `runtime_abi.py` and
`runtime_plugins.py`, each re-exporting only the name its probe uses. The
structure lint (scripts/structure/shared_shell.py, Rule 7 in
scripts/lint/code_structure.py) enforces that whitelist and that no tracked
Python file imports `shared`.

Retirement: once every cluster has run a post-rename release and no longer
needs to prepare a pre-rename image (no rollback crosses the rename), one small
PR switches the probe strings to `base.*` and deletes the two re-export modules.
The identity member does not retire in that same step: moving it is its own
expand-contract (readers accept both `base/` and `shared/` first, then the
builder writes `base/`), and only then can `shared/` be deleted.
"""
