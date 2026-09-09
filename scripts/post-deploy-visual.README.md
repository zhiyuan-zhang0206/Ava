# Post-deploy visual gate

`scripts/post_deploy_visual_check.py` is the read-only, non-blocking visual
regression gate that runs against the production frontend after a deployment
wave. It writes artifacts and exit codes only; the invoking agent routes
notifications (P0 -> `send_message` to #3242 and #405, P2 -> `notify` queue).

## Matrix

- Five surfaces: login, home (sidebar + inspector + composer), fleet,
  control, run-timeline (`/insights/run/1`).
- Twenty combinations: 5 surfaces x 2 viewports (1280x800 desktop, 390x844
  narrow) x 2 themes (light/dark).
- Every combination runs the shared structural probes (horizontal document
  overflow, in-viewport visibility, control occlusion via `elementFromPoint`,
  non-empty content blocks) after an explicit settle predicate (no visible
  `aria-busy` / `.animate-spin` / skeleton / loading elements). Any
  structural failure is P0 (exit 20).
- Pixel comparison is confined to static crops: login-card, control-header,
  control-nav, home-header, home-composer, and home-sidebar (desktop only) -
  44 captures per wave. Fleet and run-timeline are data-driven surfaces:
  structural assertions on fixed fixtures only, never pixel-diffed.

## Pixel policy

- Two frames ~1s apart per crop; only regions diffed in both frames count.
- Thresholds match CI: 0.1% changed-pixel ratio and channel delta 16,
  applied regionally.
- Attribution: the wave diff is `git log <golden>..<wave> -- ui/web`; a
  drifted crop is expected iff its surface path prefixes intersect that diff,
  otherwise unexpected.
- Escalation: single-wave unexpected drift is P2 (exit 10); the same surface
  unexpected on two consecutive deployment waves is P0. The daily 07:30
  sentinel (`--check`) reports P2 but never advances the wave counter; it
  counts as a wave only when the gateway process `started_at` changed since
  the last run. Deployment lag (a UI change merged earlier but only now
  served) surfaces as unexpected P2 - an intentional conservative direction
  to triage, not a bug to "fix".
- A crop whose golden capture does not exist yet is reported as
  `baseline-missing` and never escalates; `--accept-wave` is how it appears.

## Golden state

- No command updates a golden implicitly. `--accept-wave <sha> --accepted-by
  <reviewer>` requires all 44 captures and zero structural failures, appends
  the reviewer, UTC timestamp, SHA, and capture list to the 0600
  `acceptance-audit.jsonl`, and resets the escalation counter.
- The known-ignore registry (`post_deploy_visual_known_ignores.json`) is
  JSON with a `version` field and changes only via PR, never at runtime.

## Read-only discipline

- The browser session aborts every non-GET request on any URL; data-surface
  GETs are served fixed fixtures; the only localStorage write is `theme`
  inside the automation profile. The auth context is pinned per surface
  (data surfaces authenticated, login always renders the form, control
  uses the real read-only auth check so a dead cookie fails loudly).
  Demo mode targets a loopback preview on a port in 3001..3100.
- Runtime budget: the browser pass runs in-process under a 28-minute
  SIGALRM budget (30-minute contract), matching the former container kill.
- Engine: the repo-pinned Playwright Chromium (`playwright==1.59.0` in
  `uv.lock`), headless on the host. No Docker. The engine is deliberately
  pinned so goldens stay comparable across runs; upgrading Playwright
  requires a golden re-accept afterwards. Prerequisite: the bundled browser
  must be present on the host (`uv run playwright install chromium`, cached
  under `~/Library/Caches/ms-playwright`).

## Health probe

`--health-url` names the gateway **origin** for the host-side wave
detection (the script appends `/api/health`). It defaults to `--base-url`,
but the gate serves the SPA wall (or proxies /api to the app, which has no
API routes) for unauthenticated /api requests, so a gate base URL needs the
explicit gateway origin: `--health-url http://<gateway>:<port>`. A base URL
that answers the gateway's health JSON (`name: gateway`) is refused up front
with the same hint.

## Cookie

`AVA_VISUAL_GATE_COOKIE_FILE` must be mode 0600 and may be Playwright
storage-state JSON, a Netscape cookie jar, or a single `name=value` line.
The runbook's curl revocation only consumes Netscape and `name=value`
formats; a storage-state JSON export must be revoked from the logged-in UI.

## Daily schedule

```
30 7 * * * AVA_VISUAL_GATE_COOKIE_FILE=/secure/ava-visual-cookies.txt /path/to/Ava/.venv/bin/python /path/to/Ava/scripts/post_deploy_visual_check.py --check --base-url https://gateway.example
```


## CI preview gate

`tests/e2e/test_preview_visual_gate.py` runs the same five-surface matrix
against the committed golden set `tests/e2e/__snapshots__/preview-gate/` on
every PR (blocking, inside the e2e shard): the production frontend build
served by the e2e stack is captured on the GitHub ubuntu runner and compared
with the goldens minted on that same runner. Generation and comparison share
one rendering environment — cross-environment references hit font/FreeType
churn (the original three-snapshot lesson), so goldens minted anywhere else
are refused (`PREVIEW_GATE_REFRESH=1` only works on linux runners).

Golden update flow: a UI change that intentionally shifts the crops fails the
gate; after a reviewer confirms the drift matches the intent, the
visual-baselines workflow (workflow_dispatch on the PR head) re-mints the
goldens on the ubuntu runner and commits them to the same PR. A structurally
broken run can never become the golden (same rule as `--accept-wave`).
QA spot-checks the committed golden set (file count, meta provenance, sampled
PNGs) before approving the re-mint PR — the 44 binary goldens are not readable
in a plain PR review, so the gate's own weakening must be caught there.
The deployment gate on macmini keeps its own machine-local goldens; the two
sets never mix.
