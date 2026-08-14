#!/usr/bin/env python3
"""Build and serve OKF D3 viz. Usage: .venv/bin/python scripts/serve_okf_viz.py [--port N] [--no-serve]"""

from __future__ import annotations

import argparse
import http.server
import os
import socket
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "scripts" / "okf-d3-template.html"

sys.path.insert(0, str(REPO))

from shared.okf_graph import build_graph_data, render_html  # noqa: E402


def build() -> dict[str, Any]:
    return build_graph_data(REPO)


def inject(data: dict[str, Any], out: Path) -> None:
    html = render_html(data, TEMPLATE.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  wrote {out} ({len(html)} bytes)", file=sys.stderr)


def serve(d: Path, port: int) -> None:
    os.chdir(d)
    h = socket.gethostbyname(socket.gethostname())
    url = f"http://{h}:{port}/okf-d3.html"
    print(f"\n  OKF D3 Viz -> {url}")
    print("  Press Ctrl+C to stop.\n")
    srv = http.server.HTTPServer(("", port), http.server.SimpleHTTPRequestHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        srv.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build and serve OKF D3 viz")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--output", default="okf-d3.html")
    ap.add_argument("--no-serve", action="store_true")
    a = ap.parse_args()
    print("Building...", file=sys.stderr)
    d = build()
    n = len(d["nodes"])
    t = len(d["treeEdges"])
    c = len(d["crossEdges"])
    print(f"  {n} nodes, {t} tree, {c} cross", file=sys.stderr)
    inject(d, REPO / a.output)
    if not a.no_serve:
        serve(REPO, a.port)


if __name__ == "__main__":
    main()
