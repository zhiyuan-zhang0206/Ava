"""Role-specific OTLP exporter configuration fragments.

Every exporter keeps a bounded in-memory sending queue: a collector restart drops
whatever was queued, and `ava trace ship` can replay a mirrored trace gap.
"""

from __future__ import annotations

BACKEND_EXPORTERS = """
  otlphttp/tempo:
    endpoint: {tempo_endpoint}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 5000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
  otlphttp/loki:
    endpoint: {loki_base}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 5000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
  otlphttp/prometheus:
    endpoint: {prom_base}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 1000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
"""


RELAY_EXPORTERS = """
  # Keep all three component IDs stable across the direct-backend -> relay
  # cutover so dashboards and collector self-metrics keep their series.
  otlphttp/tempo:
    endpoint: {endpoint}
    headers:
      Authorization: {authorization}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 5000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
  otlphttp/loki:
    endpoint: {endpoint}
    headers:
      Authorization: {authorization}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 5000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
  otlphttp/prometheus:
    endpoint: {endpoint}
    headers:
      Authorization: {authorization}
    tls:
      insecure: true
    timeout: 5s
    sending_queue:
      enabled: true
      queue_size: 1000
      block_on_overflow: false
      wait_for_result: false
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 15m
"""
