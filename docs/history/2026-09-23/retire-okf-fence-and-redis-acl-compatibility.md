# Retire two compatibility branches after fleet verification

The 2026-09-23 fleet scan found no spaced OKF fences in the repository,
registered machine homes, or AvaVault. OKF graph parsing now uses the shared
typed frontmatter parser alone. A spaced fence is plain body text, as is an
invalid or unclosed bare-fence block. This keeps the graph adapter's documented
lenient contract while removing a second YAML parse path.

The same scan found usernames in both live Redis URLs; other machines carry no
Redis URL. Converge already backfills the username during upgrade. The Redis ACL
healthcheck therefore no longer skips a username-less URL: identity resolution
raises `ValueError` for that misconfiguration. The no-secret liveness path and
ACL re-affirmation remain in place.
