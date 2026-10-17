from base.packages.plugins.config_registration import disk_image_path, read_plugin_config
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

config = read_plugin_config("meiva_fleet", FleetConfig, disk_image_path("meiva_fleet"))
assert config.task_escalate_n == 7, config.task_escalate_n
assert config.reduce_context_switch is False
print("gateway-unit-compose-ok")
