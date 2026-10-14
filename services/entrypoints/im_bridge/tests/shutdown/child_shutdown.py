"""Run the im_bridge daemon's bounded shutdown probe."""

from base.deploy.schema.tests.schema_gate import run_child
from services.entrypoints.im_bridge import daemon

if __name__ == "__main__":
    run_child(daemon)
