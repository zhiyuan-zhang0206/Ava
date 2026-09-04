# Restricted updater bootstrap rebuild

PR #1536 was reconstructed on top of the merged native-launcher observation and
transport-encryption work instead of rebasing its inherited historical stack.
The retained scope is the restricted immutable `ava-ops` A-to-B updater hop and
its native-reader bridge. Plugin discovery, PTY lifecycle, and unrelated branch
changes are deliberately excluded.

The reconstruction closes five review blockers before the hop can be considered
a trustworthy bootstrap primitive:

- resume recollects the real unit inventory and permits only the independently
  verified single `ava-ops` A/B process identity to change;
- first ownership transfer CAS-checks the exact dead predecessor handoff rather
  than replacing whatever stale marker happens to be present;
- compensation evidence uses a bounded version-1 sidecar whose malformed bytes
  are retained for audit, while unrelated malformed ordinary handoff state stays
  recoverable;
- a launch exception before a new exact session record remains ambiguous and
  cannot trigger a second launch; and
- phase evidence and its encoded file have explicit count and byte budgets.

The restricted child projection continues to carry the transport-encryption
declaration and the bootstrap observer continues to enforce the secure off-box
bind contract introduced by PR #1524. The result remains a bootstrap-only slice,
not permission for normal service activation or deployment.
