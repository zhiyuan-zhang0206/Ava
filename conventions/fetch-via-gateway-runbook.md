# Central fetch protocol retirement

The old central-fetch rollout protocol has no supported operator entry point.
Its `cluster_fetch` RPC, runner update RPC, preparation RPCs, and bootstrap
continuation RPCs have been removed. The ops client and daemon reject those
requests before dispatch; setting `fetch_via_gateway` cannot restore them.

The old topology helpers and configuration field remain with internal updater
code pending deletion. They are not a rollout or restart path. Generic
configuration, pause, resume, and status operations remain available.

Prepared release images and the native release executor are described in
[the unified lifecycle plan](../future/infra/unified-cluster-lifecycle.md).
That implementation does not yet replace the old multi-machine central-fetch
policy. Fleet image distribution remains pre-cutover work; there is no
compatibility path through the retired RPCs.
