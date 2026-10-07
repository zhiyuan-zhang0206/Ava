"""Notice transaction owners and the guarded current-notice HTTP surface.

`receipts` owns keyed creation/resolution storage; `current` owns observed-row
edit/withdraw acceptance; `router` mounts those versioned operations. Legacy
feeds and selectors stay in `gateway.agents.notices`.
"""
