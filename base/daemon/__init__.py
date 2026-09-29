"""Service daemon machinery: the health endpoint and its envelope, the
settings-free HTTP transport, graceful shutdown, loop liveness, and the
in-cluster schedules and watchers.

Members: `health` (the shared `/healthz` endpoint, health ports, liveness),
`health_schema` (the envelope that names the component to restart),
`http_transport` (the settings-free HTTP server `health` rides on), `shutdown`
(stop signals through cleanup), `loop_health` (concurrent-loop progress), and
`schedules/` (built-in schedules, watchers, completion notices, schedule timing).

Docstring-only door: import the member module you need.
"""
