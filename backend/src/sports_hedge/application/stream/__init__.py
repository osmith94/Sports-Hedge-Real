"""STREAM Phase 1: one-fixture Polymarket market-feed shadow surveillance.

Default OFF. Read-only. Does not replace HOT/BACKGROUND/UNIVERSE or Price-2.
"""

from sports_hedge.application.stream.protocol import STREAM_LANE
from sports_hedge.application.stream.runtime import (
    get_stream_runtime,
    reset_stream_runtime,
    StreamRuntime,
)

__all__ = ["STREAM_LANE", "StreamRuntime", "get_stream_runtime", "reset_stream_runtime"]
