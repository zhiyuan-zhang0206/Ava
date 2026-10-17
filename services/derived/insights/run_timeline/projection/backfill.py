# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#bounded-projection-preparation
from __future__ import annotations

import argparse

from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from services.derived.insights.run_timeline.projection.jobs import index_next


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare derived Run Timeline rows for the named agents using the installed generation."
    )
    parser.add_argument("agent_ids", nargs="+", type=int)
    args = parser.parse_args()
    if any(agent_id <= 0 for agent_id in args.agent_ids):
        parser.error("agent ids must be positive")
    config = ConfigBoot()
    config.boot()
    version = CodeVersion(LoadedCommit.capture())
    db = Database(
        db_config_from_boot(config),
        gate=ProcessDbGate(
            version=version.get,
            process="timeline-projection-backfill",
        ),
    )
    inputs = TimelineReadInputs(
        lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        lambda: config.view.general.message_timestamps,
    )
    pending = list(dict.fromkeys(args.agent_ids))
    counts = dict.fromkeys(pending, 0)
    while pending:
        for agent_id in list(pending):
            if index_next(db, agent_id, inputs=inputs, read_segment=read_checkpoint_segment):
                counts[agent_id] += 1
            else:
                pending.remove(agent_id)
                logger.info(
                    "timeline projection ready: agent {agent}, added {count} segments",
                    agent=agent_id,
                    count=counts[agent_id],
                )


if __name__ == "__main__":
    main()
