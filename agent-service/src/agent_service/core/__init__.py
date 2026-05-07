"""Role-agnostic agent core. Drivers (chat today, monitor / pulse /
peer-consult later) normalize their world into a `TurnEnvelope` and
implement a `TurnSink` for transport. The core's loop body knows
about neither.

The design rule, taken from `AGENTS.md`: the foundation here does
NOT speculate about which drivers will exist. New drivers plug in
by providing a builder function (driver world → `TurnEnvelope`) and
a `TurnSink` implementation; nothing in this package needs to change.

The chat driver `agent_service.loop_driver.run_turn` is today's
single consumer of `run_one_turn`. Future drivers compose the same
function with their own envelope builder and sink.
"""

from agent_service.core.envelope import TurnEnvelope
from agent_service.core.run import TurnOutcome, resolve_run_type, run_one_turn
from agent_service.core.sink import SseSink, TurnSink

__all__ = [
    "SseSink",
    "TurnEnvelope",
    "TurnOutcome",
    "TurnSink",
    "resolve_run_type",
    "run_one_turn",
]
