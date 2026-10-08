"""The insights service: read models over agent history, in a process of their own.

An independent daemon (`python -m services.derived.insights.daemon`) that answers on the
Unix socket `base.paths.insights_socket()`. The gateway authenticates the caller and
reverse-proxies the read routes to it (`gateway/routers/insights.py`); the browser never
dials it. Building a read model is CPU-bound (rebuilding an agent's stitched history takes
seconds), so it runs here, off the gateway's event loop and thread pool.

Package door — no imports, no re-exports. Modules:

  - `app.py`      — `build_app`: the FastAPI app and the state its routers read
  - `daemon.py`   — pidfile, `/healthz`, the socket and the uvicorn server
  - `run_timeline/` — the single-agent run timeline reads
"""
