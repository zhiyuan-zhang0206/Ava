"""Role-specific OTLP exporter configuration fragments.

Exporter component IDs remain stable across backend and relay shapes because
the file-storage extension keys persistent queues by component ID.
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
      storage: file_storage
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
      storage: file_storage
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
  # cutover. In particular, file_storage keys the Tempo/Loki persisted queues
  # by exporter ID; renaming either would strand the backlog this repair drains.
  # Prometheus stays stable too, while retaining its bounded in-memory policy.
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
      storage: file_storage
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
      storage: file_storage
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
