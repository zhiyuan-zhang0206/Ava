"""Run the delivery_watchdog daemon's bounded shutdown probe."""

from base.deploy.schema.tests.schema_gate import run_child
from services.wake.delivery_watchdog import daemon

if __name__ == "__main__":
    run_child(daemon)
