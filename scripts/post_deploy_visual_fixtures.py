"""Fixed browser fixtures for data-dominated post-deploy surfaces."""

AGENT = {
    "agent_id": 1,
    "spawner": "user",
    "status": "idling",
    "pid": 100,
    "spawned_at": "2026-09-01T00:00:00Z",
    "started_at": "2026-09-01T00:00:01Z",
    "last_active_at": "2026-09-01T00:00:02Z",
    "label": "visual fixture agent",
    "machine": "visual-host",
    "supports_vision": True,
    "notices_awaiting_response": [],
    "unread_notice_count": 0,
    "fork_source_agent_id": None,
    "heartbeat_paused_until": None,
    "liveness_state": "online",
}
HEARTBEAT = {
    "interval_s": 300,
    "next_at": None,
    "paused_until": None,
    "heartbeat_pending": False,
    "last_pause": None,
}
INSPECT_LIVE = {
    "agent_id": 1,
    "machine": "visual-host",
    "liveness_state": "online",
    "last_probe_at": None,
    "shells_available": True,
    "spawned_at": "2026-09-01T00:00:00Z",
    "started_at": "2026-09-01T00:00:01Z",
    "shells": [],
    "config_overlay": {},
    "notice": None,
    "heartbeat": HEARTBEAT,
}
INSPECT = {
    **INSPECT_LIVE,
    "window_hours": 24,
    "applied_window_hours": 24,
    "since_compact": False,
    "cost": {
        "cost_usd": 0.25,
        "unpriced_calls": 0,
        "llm_calls": 4,
        "tokens_in": 1000,
        "tokens_out": 200,
        "tokens_cached": 500,
        "tokens_reasoning": 50,
        "cache_hit_pct": 50.0,
    },
    "stats": {
        "turn_total": 4,
        "turn_ok": 4,
        "turn_p50_seconds": 2.0,
        "turn_p90_seconds": 3.0,
        "turn_min_seconds": 1.0,
        "turn_max_seconds": 4.0,
        "exec_ok": 2,
        "exec_failed": 0,
    },
    "tps": {"lm_stage_tps": 20.0, "agent_lifecycle_tps": 10.0},
    "activity": {
        "active_seconds": 60,
        "alive_seconds": 120,
        "active_rate": 0.5,
        "llm_seconds": 40,
        "exec_seconds": 10,
    },
}
RUN_TIMELINE = {
    "agent_id": 1,
    "window": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-01T01:00:00Z"},
    "meta": {
        "n_turns": 1,
        "wall_span_s": 3600,
        "active_s": 4,
        "tokens_in": 100,
        "tokens_out": 20,
        "cost_usd": 0.01,
        "n_exec_failed": 0,
        "n_compact": 0,
        "n_restart": 0,
        "fallback_turns": 0,
        "unmatched_turns": 0,
    },
    "rows": [
        {
            "turn": 1,
            "n_turns": 1,
            "start": "2026-09-01T00:10:00Z",
            "end": "2026-09-01T00:10:04Z",
            "active_s": 4,
            "trace_id": "visual-trace",
            "checkpoint_id": None,
            "ok": True,
            "llm": {
                "calls": 1,
                "in_total": 100,
                "cache_read": 50,
                "out_total": 20,
                "reasoning": 5,
                "latency_ms": 1200,
                "cost_usd": 0.01,
                "model": "visual-model",
            },
            "execs": [],
            "anomalies": [],
            "tags": [],
        }
    ],
    "events": [],
    "boundaries": {
        "initialize_turn": 1,
        "last_before_compact_turn": 1,
        "post_window_turns": 0,
        "has_activity_after_window": False,
    },
}
FIXTURES: dict[str, object] = {
    "/api/agents": [AGENT],
    "/api/notices": {"open": [], "awaiting": [], "resolved_page": [], "next_cursor": None},
    "/api/tasks": {"tasks": []},
    "/api/agents/1/timeline": {"items": [], "msg_count": 0, "has_more": False},
    "/api/agents/1/token-usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "max_context_tokens": 10000,
        "soft_compact_tokens": 7000,
        "hard_compact_tokens": 9000,
    },
    "/api/agents/1/pages": [],
    "/api/agents/1/pending": [],
    "/api/pages": [],
    "/api/commands": [],
    "/api/fleet/graph": {
        "nodes": [
            {
                "agent_id": 1,
                "label": "visual fixture agent",
                "status": "idling",
                "liveness_state": "online",
                "spawner": "user",
                "machine": "visual-host",
                "node_score": 0,
                "total_tokens": 1200,
            }
        ],
        "edges": [],
        "stale": False,
        "truncated": False,
        "telemetry_stale": False,
        "snapshot_at": None,
    },
    "/api/settings": {
        "settings": [
            {
                "key": "display.inspector_open",
                "value": True,
                "updated_at": "2026-09-01T00:00:00Z",
            }
        ]
    },
    "/api/agents/1/inspect": INSPECT,
    "/api/agents/1/inspect/live": INSPECT_LIVE,
}
INERT_EVENT_SOURCE = """
class InertEventSource {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  constructor(url) {
    this.url = url; this.readyState = InertEventSource.CONNECTING;
    this.onopen = null; this.onmessage = null; this.onerror = null;
    setTimeout(() => {
      if (this.readyState === InertEventSource.CLOSED) return;
      this.readyState = InertEventSource.OPEN;
      if (this.onopen) this.onopen(new Event("open"));
    }, 0);
  }
  close() { this.readyState = InertEventSource.CLOSED; }
  addEventListener() {}
  removeEventListener() {}
}
window.EventSource = InertEventSource;
"""
