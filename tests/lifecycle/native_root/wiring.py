"""Native fixture assembly: real terminal resource broker, no Gateway or diagnostics."""

from services.ava_root.wiring import WiringContext, WiringParticipant
from services.ava_root_glue.windows_terminal import TerminalBroker


def build(context: WiringContext) -> list[WiringParticipant]:
    return [TerminalBroker(context)]
