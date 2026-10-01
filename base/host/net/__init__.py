"""Network primitives: config-free host and URL predicates (`predicates`),
httpx dials that pin IPv4-literal targets to AF_INET (`http_dial`), the shared
retry loop (`resilience`), data-plane URL userinfo rewriting and redaction
(`url_secret`), and the guard that keeps httpx's CLI dependencies out of the
process (`httpx_cli_guard`).

A door that imports nothing heavy: `url_secret` is imported by the Settings load
itself and `predicates` by first-start paths that run before any settings, so
importing the package pulls no dependency — import the member module you need.
The one thing it does is run `block_httpx_cli()`: the guard must run before the
first `import httpx` of the process, and every service entry imports this package
(through the Settings load) before anything reaches httpx, as does `http_dial`.
"""

from base.host.net.httpx_cli_guard import block_httpx_cli

block_httpx_cli()
