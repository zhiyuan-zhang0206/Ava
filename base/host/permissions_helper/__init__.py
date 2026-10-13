"""Client side of the macOS permissions helper's socket protocol.

`wire` is the request/result vocabulary and `client` the `PermissionsHelperClient`: one
object per helper socket, whose methods are the calls (spawn, ping, capability checks,
input and observation). `client_stateful` holds the calls whose effect outlives the call;
the client inherits them. Nothing in this package reads configuration: a composition root
builds the client (`base.sessions.helperproc.helper_client`) and hands it to its callers.
The helper daemon itself, its launchd job and its bring-up live in
`services/desktop/permissions_helper`.
"""
