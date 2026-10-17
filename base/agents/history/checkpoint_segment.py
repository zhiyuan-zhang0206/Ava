# meiva:ref base/agents/history/docs/range-read-contracts.meiva.okf.md#history-range-inputs
from __future__ import annotations

from typing import cast

from psycopg.rows import dict_row

from base.agents.history.checkpoint import CheckpointReadError, FullHistory, single_segment_history
from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver
from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.agents.history.checkpoint_serializer import CheckpointSerializer
from base.agents.history.closing_request import METADATA_KEY, ClosingRequest
from base.agents.history.delta_read_compat import reconstruct_delta_messages
from base.db import Database


def read_checkpoint_segment(db: Database, agent_id: int, checkpoint_id: str) -> FullHistory:
    serde = CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    pool = db.pool(autocommit=True, row_factory=dict_row)
    try:
        with pool.connection() as conn:
            saver = HistoryPostgresSaver(conn=conn, serde=serde)
            value = saver.get_tuple(
                {
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                        "checkpoint_id": checkpoint_id,
                    }
                }
            )
            if value is not None:
                reconstruct_delta_messages(saver, value)
    except CheckpointReadError:
        raise
    except Exception as exc:
        raise CheckpointReadError(
            f"checkpoint {checkpoint_id} read failed for agent {agent_id}"
        ) from exc
    finally:
        pool.close()
    if value is None:
        raise CheckpointReadError(f"checkpoint {checkpoint_id} missing for agent {agent_id}")
    history = single_segment_history(value.checkpoint["channel_values"].get("messages", []))
    anchor = cast(dict[str, object], value.metadata).get(METADATA_KEY)
    return history._replace(segment_closings=(ClosingRequest.from_metadata(anchor),))
