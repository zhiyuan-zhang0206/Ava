from __future__ import annotations

import sys

from base.packages import plugin_providers
from base.packages.plugins.config_registration import validate_config_overlay

assert not hasattr(plugin_providers, "_STATE"), "catalog has no ambient holder"
catalog = plugin_providers.build_model_catalog()
validate_config_overlay({"llm_model": "deepseek-flash"}, models=catalog.models)
sys.stdout.write("ok\n")
