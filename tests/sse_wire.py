"""SSE wire builders for tests — the ``data:``-framed byte fixtures the decoder consumes.

The OpenAI-compatible streaming wire wraps each chunk as ``data: <json>\\n\\n``. These helpers
own that framing once, so every test speaks one faithful wire shape (Boundary Fixture Fidelity)
instead of each module re-encoding the ``data:``/blank-line/``encode`` idiom. They build BYTES
for a real decoder to parse — never a shortcut around it.
"""

from __future__ import annotations

import json


def sse_frame(payload: str) -> bytes:
    """One SSE frame: a ``data:`` line carrying ``payload`` plus the blank separator."""
    return f"data: {payload}\n\n".encode()


def sse_frame_json(payload: dict[str, object]) -> bytes:
    """An SSE frame whose ``data:`` line is ``payload`` as JSON — the common chunk shape."""
    return sse_frame(json.dumps(payload))
