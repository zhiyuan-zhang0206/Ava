"""Client side of the macOS permissions helper's socket protocol.

`wire` is the request/result vocabulary, `client` the calls (spawn, ping, capability checks,
input and observation), `client_stateful` the calls whose effect outlives the call. The helper
daemon itself, its launchd job and its bring-up live in `services/desktop/permissions_helper`;
callers at every layer reach the helper through this package.
"""
