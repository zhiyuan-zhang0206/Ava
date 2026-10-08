# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns
- **The catch-all boundary**:`except Exception: return 502` in a proxy — a defined 404 becomes "app down", browsers cache stale content forever, and the user sees a deleted icon "resurrect" (gate favicon incident) → alternative: pass through defined status codes and bodies; degrade to 502 only on transport failure.
- **The secret in the log line**:debug-logging a token "temporarily" or committing `.env` "just this once" — logs and repos are reachable without the secret, unlike the DB → alternative: env injection + default redaction + commit scanning; treat every leaked secret as already public and rotate.
- **The credential aimed at the wrong endpoint**:hardcoding `127.0.0.1:{port}` as the login target — remote clients POST the cluster secret to their own loopback and the request never arrives; worse, the fix was silently reverted by a rebase and shipped in main and prod (gate login regression) → alternative: derive endpoints from machine configuration, and pin the security property with a regression test that fails if the fix disappears.
- **The unexpired cache**:deleting an asset but leaving browsers/proxies holding it — removal is not complete until caches are handled → alternative: explicit cache-control/expiry on removed assets; verify deletion end-to-end (direct curl to the path, not the cached tab).
- **The floating dependency**:`dep>=x` or unpinned installs — supply chain arrives unreviewed and unversioned → alternative: lock files committed, new dependencies reviewed, CVE tracking.
- **Security as a wrap-up phase**:"we'll harden after launch" — retrofit security is skipped or rushed precisely when the system is biggest → alternative: threat model at design; security as a standing review checklist item.

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th anniv. ed.), Tips 72–73 (Minimize Attack Surface / Patch Early), Tip 8 (Good-Enough Software) — `references/03-pragmatic-programmer.md §8.4`
- Ava incident records (memory pool): `ava/bugs/gate-404-to-502-favicon-resurrection.md` (catch-all swallowed 4xx→502, fixed in PR #1294); `ava/bugs/gate-login-loopback-regression-2026-08-03.md` (security fix silently reverted by a rebase, replayed in PR #1261); `ava/design/user-ruling-db-secrets-cleartext-20260802.md` (user ruling: cleartext at rest acceptable; never in logs or external services); `infra/security/redis-password-rotated.md` (requirepass rotation synced with consumers)
- Addy Osmani, *agent-skills*: `security-and-hardening` — production-grade security review skill (see `research/agent-skills-ecosystem.md`)
- OWASP Top 10 (2021) — <https://owasp.org/www-project-top-ten/>
