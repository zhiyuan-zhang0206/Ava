---
type: doc
title: IM Bridge Feishu lifecycle
description: Service ownership and best-effort shutdown of the Feishu SDK worker.
tags:
- im-bridge
- lifecycle
---

# Feishu lifecycle

`services/entrypoints/im_bridge/adapters/feishu.py` starts the blocking SDK
websocket client in a service-owned executor task. Worker entry acknowledges
startup; active callback faults and unexpected worker exits reach the same
service task scope.

Shutdown rejects callbacks, cancels the service wait, and submits a best-effort
private SDK disconnect request on the SDK loop. It does not wait for that loop to
execute or finish the request, or join the worker. The daemon exits after owned
cleanup even when the SDK loop is unresponsive.

`services/entrypoints/im_bridge/tests/test_feishu_worker_exit.py` runs the real
daemon main in child processes and retains the 10-second exit bound. A responsive
fixture explicitly acknowledges request execution before observing its marker;
a separate wedged-loop fixture proves cleanup and exit without that execution.
Neither fixture requires the SDK worker to finish.
