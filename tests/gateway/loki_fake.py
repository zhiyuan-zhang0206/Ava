"""In-memory Loki stand-in shared by the gateway read-side tests.

Rows are added with `add(...)` (the same shape the old `events` INSERTs had);
the fake honors the same filter/window/paging semantics `gateway.loki_events`
relies on (categories, event_name regex, attribute filters, exclude_agent_ids,
from_/to, newest-first, +1 lookahead has_more, forward direction, projected
line_format rows, grouped counts). `count_events` / `count_grouped` /
`query_events` / `query_projected_lines` mirror the real module's signatures
so a test can `monkeypatch.setattr("gateway.loki_events.<fn>", fake.<fn>)` or
pass the instance straight to `fetch_aggregate(..., loki=fake)`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any


class FakeLoki:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(
        self,
        *,
        event: str,
        agent_id: int | None = None,
        target_agent_id: int | None = None,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
        category: str = "telemetry",
    ) -> None:
        row = {
            "id": len(self.rows) + 1,
            "ts": datetime.now(UTC) - timedelta(hours=ts_offset_hours),
            "agent_id": agent_id,
            "machine": "test",
            "process": "test",
            "category": category,
            "event_name": event,
            "level": level,
            "source": "test",
            "target_agent_id": target_agent_id,
            "attributes": payload or {},
        }
        self.rows.append(row)

    def _match(self, **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        agent_id: Any = kwargs.get("agent_id")
        excluded: Any = kwargs.get("exclude_agent_ids")
        categories: Any = kwargs.get("categories")
        event_names: list[str] = kwargs.get("event_names") or []
        attribute_filters: dict[str, str] = kwargs.get("attribute_filters") or {}
        cluster: Any = kwargs.get("cluster")
        from_: Any = kwargs.get("from_")
        to: Any = kwargs.get("to")
        grep: Any = kwargs.get("grep")
        for r in self.rows:
            if agent_id is not None and r["agent_id"] != agent_id:
                continue
            if excluded and r["agent_id"] in excluded:
                continue
            if kwargs.get("service_only") and r["agent_id"] is not None:
                continue
            if categories and r["category"] not in categories:
                continue
            if cluster is not None and r.get("cluster") not in (None, cluster):
                continue
            if event_names and not any(re.search(e, r["event_name"]) for e in event_names):
                continue
            if kwargs.get("level") is not None and r["level"] != kwargs["level"]:
                continue
            if kwargs.get("level_min") is not None:
                levels = ("debug", "info", "warning", "error", "critical")
                if levels.index(r["level"]) < levels.index(kwargs["level_min"]):
                    continue
            matched = True
            for key, want in attribute_filters.items():
                # normalize like jsonb `->>'key'` does: True -> "true"
                raw = r["attributes"].get(key, "")
                got = json.dumps(raw).strip('"') if raw != "" else ""
                want_s = str(want)
                if want_s.startswith("!="):
                    if got == want_s[2:]:
                        matched = False
                        break
                elif got != want_s:
                    matched = False
                    break
            if not matched:
                continue
            if from_ is not None and r["ts"] < from_:
                continue
            if to is not None and r["ts"] > to:
                continue
            if grep and grep not in json.dumps(r, default=str):
                continue
            out.append(r)
        return out

    # ── gateway.loki_events surface ────────────────────────────────────────

    def count_events(self, **kwargs: Any) -> int:
        return len(self._match(**kwargs))

    def count_grouped(self, **kwargs: Any) -> dict[str, int]:
        group_by = kwargs["group_by"]
        from_attributes = kwargs.get("from_attributes", False)
        exclude_empty = kwargs.get("exclude_empty", False)
        counts: dict[str, int] = {}
        for r in self._match(**kwargs):
            if from_attributes:
                k = str(r["attributes"].get(group_by, ""))
            elif group_by == "agent_id":
                k = str(r["agent_id"]) if r["agent_id"] is not None else ""
            else:
                k = str(r.get(group_by, ""))
            if exclude_empty and k == "":
                continue
            counts[k] = counts.get(k, 0) + 1
        return counts

    def count_events_series(self, **kwargs: Any) -> dict[str, list[tuple[int, int]]]:
        """Per-step event counts on the caller's step grid (bucket END
        times aligned to `from_`), grouped like count_grouped."""
        step_s = kwargs["step_s"]
        from_: Any = kwargs.get("from_")
        to: Any = kwargs.get("to")
        group_by: Any = kwargs.get("group_by")
        from_attributes = kwargs.get("from_attributes", False)
        base = int(from_.timestamp()) if from_ is not None else 0
        acc: dict[str, dict[int, int]] = {}
        for r in self._match(**kwargs):
            if from_ is not None and r["ts"] < from_:
                continue
            if to is not None and r["ts"] > to:
                continue
            t = int(r["ts"].timestamp())
            end_ts = base + ((t - base) // step_s + 1) * step_s
            if from_attributes:
                k = str(r["attributes"].get(group_by, ""))
            elif group_by == "agent_id":
                k = str(r["agent_id"]) if r["agent_id"] is not None else ""
            else:
                k = str(r.get(group_by, ""))
            bucket = acc.setdefault(k, {})
            bucket[end_ts] = bucket.get(end_ts, 0) + 1
        return {k: sorted(pts.items()) for k, pts in acc.items()}

    def attribute_max_series(self, **kwargs: Any) -> list[tuple[int, float]]:
        """Per-step max of a numeric attribute (bucket END times aligned to
        `from_`), mirroring gateway.loki_events.attribute_max_series."""
        step_s = kwargs["step_s"]
        from_: Any = kwargs.get("from_")
        to: Any = kwargs.get("to")
        field = kwargs["field"]
        base = int(from_.timestamp()) if from_ is not None else 0
        acc: dict[int, float] = {}
        for r in self._match(**kwargs):
            if from_ is not None and r["ts"] < from_:
                continue
            if to is not None and r["ts"] > to:
                continue
            v = r["attributes"].get(field)
            if v is None:
                continue
            t = int(r["ts"].timestamp())
            end_ts = base + ((t - base) // step_s + 1) * step_s
            acc[end_ts] = max(acc.get(end_ts, float("-inf")), float(v))
        return sorted(acc.items())

    def query_events(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        direction = kwargs.get("direction", "backward")
        rows = sorted(
            self._match(**kwargs), key=lambda r: r["ts"], reverse=(direction == "backward")
        )
        limit = kwargs.get("limit", 100)
        offset = kwargs.get("offset", 0)
        page = rows[offset : offset + limit]
        return page, len(rows) > offset + limit

    def attribute_aggregate(self, **kwargs: Any) -> float | list[tuple[str, float]]:
        """Scalar sum/count over one payload attribute, mirroring
        gateway.loki_events.attribute_aggregate for the agg/field/filters the
        dashboard reads use (sum + count; group_by returns the grouped list)."""
        field = kwargs["field"]
        agg = kwargs["agg"]
        group_by: Any = kwargs.get("group_by")
        rows = self._match(**kwargs)
        acc: dict[str, float] = {}
        for r in rows:
            if agg == "count":
                v = 1.0
            else:
                raw = r["attributes"].get(field)
                if raw is None:
                    continue
                v = float(raw)
            key = str(r["attributes"].get(group_by, "")) if group_by else ""
            acc[key] = acc.get(key, 0.0) + v
        if group_by:
            return list(acc.items())
        return acc.get("", 0.0)

    def query_projected_lines(self, **kwargs: Any) -> list[tuple[int, int | None, str]]:
        fields = kwargs["fields"]
        template = kwargs["template"]

        def _render(r: dict[str, Any]) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1)
                if name.startswith("len "):
                    inner = name[4:].lstrip(".")
                    return str(len(str(_value(r, inner))))
                return str(_value(r, name.lstrip(".")))

            return re.sub(r"\{\{\s*(.*?)\s*\}\}", repl, template)

        def _value(r: dict[str, Any], name: str) -> Any:
            if name in fields:
                return r["attributes"].get(name, "")
            if name == "event_name":
                return r["event_name"]
            if name == "agent_id":
                return r["agent_id"] if r["agent_id"] is not None else ""
            return ""

        out: list[tuple[int, int | None, str]] = []
        for r in self._match(**kwargs):
            ts_ns = int(r["ts"].timestamp() * 1e9)
            out.append((ts_ns, r["agent_id"], _render(r)))
        out.sort(key=lambda x: x[0])
        return out
