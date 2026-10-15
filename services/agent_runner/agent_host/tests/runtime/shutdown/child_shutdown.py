from base.deploy.schema.tests.schema_gate import run_child
from services.agent_runner.agent_host import daemon

if __name__ == "__main__":
    run_child(daemon)
