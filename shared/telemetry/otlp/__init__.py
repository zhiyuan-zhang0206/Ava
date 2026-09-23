"""OTLP export backend package (task #4555 R3).

Modules moved here from the former root-level `telemetry_otlp*.py` family:
`telemetry_otlp` (the backend), `telemetry_otlp_defer` (exec-child deferral),
`telemetry_otlp_gauges`, `telemetry_otlp_logs`, and `telemetry_otlp_metrics`.
Their `telemetry-otlp/` doc nodes moved with them.

This init is intentionally docstring-only: importing the backend pulls the
full OTel SDK stack, and the exec-child deferral path depends on nothing here
loading implicitly — import the submodule you need directly
(`from shared.telemetry.otlp.telemetry_otlp import ...`).
"""
