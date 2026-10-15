import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from base.packages.dashboard_supply import collect_plugin_specs
from base.telemetry.metrics.core import catalog
from base.telemetry.metrics.grafana_dashboard import render_dashboard, render_to_json

plugins = collect_plugin_specs()
core = catalog.collect_core_metrics()
digest = hashlib.sha256(render_to_json(render_dashboard(core, plugins.specs)).encode()).hexdigest()
sys.stdout.write(digest + "\n")
