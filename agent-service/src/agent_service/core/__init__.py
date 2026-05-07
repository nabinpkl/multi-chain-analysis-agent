"""Role-agnostic agent core. Drivers (chat today, monitor / pulse /
peer-consult later) normalize their world into a `TurnEnvelope` and
implement a `TurnSink` for transport. The core's loop body knows
about neither.

The design rule, taken from `AGENTS.md` (no god component, no dead
code stubbed for the future): the foundation here does NOT speculate
about which drivers will exist. New drivers plug in by providing a
builder function (driver world → `TurnEnvelope`) and a `TurnSink`
implementation; nothing in this package needs to change.

This module is the foundation only (types). The extracted loop body
arrives in `core.run` next; today's `agent_service.loop_driver.run_turn`
still owns the body and will move in the follow-on commit.
"""

from agent_service.core.envelope import TurnEnvelope
from agent_service.core.sink import SseSink, TurnSink

__all__ = ["TurnEnvelope", "TurnSink", "SseSink"]
