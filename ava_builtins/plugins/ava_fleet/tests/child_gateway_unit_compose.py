from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
from base.packages.plugins.config_registration import disk_image_path, read_plugin_config

config = read_plugin_config("ava_fleet", FleetConfig, disk_image_path("ava_fleet"))
assert config.task_escalate_n == 7, config.task_escalate_n
assert config.reduce_context_switch is False
print("gateway-unit-compose-ok")
