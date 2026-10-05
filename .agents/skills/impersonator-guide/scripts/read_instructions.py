"""Print the native instructions inherited by an active impersonator."""

import argparse
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, SystemMessage

from ava import external
from ava.external import state as snapshots
from base.agents.messages.kwargs import AvaMsgType, NoteTag, read_ava_kwargs


def instruction_text(messages: Sequence[BaseMessage]) -> str:
    """Select the native system prompt and configured preloaded skill notes."""
    if not messages or not isinstance(messages[0], SystemMessage):
        raise RuntimeError("borrowed agent has no saved system prompt")
    parts: list[str] = []
    for index, message in enumerate(messages):
        metadata = read_ava_kwargs(message)
        if index != 0 and not (
            metadata.get("ava_msg_type") == AvaMsgType.SYSTEM_NOTE
            and metadata.get("ava_note_tag") == NoteTag.PRELOADED_SKILLS
        ):
            continue
        if not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("borrowed agent instructions must contain nonempty text")
        parts.append(message.content)
    return "\n\n".join(parts)


def read_instructions(session_id: int, agent_id: int) -> str:
    """Read native guidance under the existing attachment's authority checks."""
    with external.attach(session_id, agent_id=agent_id):
        # ava.state also includes external journal replay. Read the native
        # snapshot separately so a controller's staged messages cannot replace
        # the inherited instructions. Attachment close revalidates the lease
        # before this function returns any text to the executor.
        snapshot, _, _ = snapshots.load_snapshot(agent_id)
        return instruction_text(snapshot.messages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--agent", required=True, type=int, dest="agent_id")
    args = parser.parse_args()
    print(read_instructions(args.session_id, args.agent_id))


if __name__ == "__main__":
    main()
