"""Notice transaction owners and the guarded current-notice HTTP surface.

`receipts` owns keyed creation/resolution storage; `current` owns observed-row
edit/withdraw acceptance; `router` mounts those versioned operations. Feeds,
creation and the resolve handler live in `gateway.agents.notices`.
"""
