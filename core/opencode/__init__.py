"""opencode HTTP client -- package root.

`core.opencode.client` owns the conversation with an `opencode serve` process,
`core.opencode.wire` the vocabulary of what it answers, and
`core.opencode.sse` (plan todo 7) the event stream. Everything a caller needs is
re-exported here, so `from core.opencode import OpencodeClient, OpencodeError` is
the whole surface.
"""

from core.opencode.client import OpencodeClient
from core.opencode.wire import (
    MessageRecord,
    OpencodeDeadlineExceeded,
    OpencodeError,
    OpencodeErrorEnvelope,
    OpencodeHealth,
    OpencodeProtocolError,
    OpencodeReply,
    OpencodeStatusError,
    SessionInfo,
)

__all__ = [
    "MessageRecord",
    "OpencodeClient",
    "OpencodeDeadlineExceeded",
    "OpencodeError",
    "OpencodeErrorEnvelope",
    "OpencodeHealth",
    "OpencodeProtocolError",
    "OpencodeReply",
    "OpencodeStatusError",
    "SessionInfo",
]
