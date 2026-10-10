"""STREAM: one-fixture Polymarket market-feed into existing Price-2.

Default OFF. Public market WS only. Does not replace HOT/BACKGROUND/UNIVERSE.
"""

from sports_hedge.application.stream.protocol import STREAM_LANE
from sports_hedge.application.stream.runtime import (
    get_stream_runtime,
    reset_stream_runtime,
    StreamRuntime,
)

__all__ = ["STREAM_LANE", "StreamRuntime", "get_stream_runtime", "reset_stream_runtime"]
