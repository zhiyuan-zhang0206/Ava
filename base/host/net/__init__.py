"""Network primitives: config-free host and URL predicates (`predicates`),
httpx dials that pin IPv4-literal targets to AF_INET (`http_dial`), the shared
retry loop (`resilience`), and data-plane URL userinfo rewriting and redaction
(`url_secret`).

Docstring-only door: `url_secret` is imported by the Settings load itself and
`predicates` by first-start paths that run before any settings, so importing
the package pulls nothing — import the member module you need.
"""
