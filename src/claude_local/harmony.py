"""The assistant's reply, read out of a channel transcript when a server leaks one.

An OpenAI-compatible server is supposed to return the assistant's user-facing message as
``content``. Some return the model's whole harmony transcript instead — the chain-of-thought in
an ``analysis`` channel, the answer in a ``final`` one, wrapped in channel markup. gpt-oss served
through mlx_vlm does exactly this, so a correct answer arrives inside markup that makes it
unparseable.

Recovering the final channel here rather than loosening the whole-file parser keeps that parser
strict: its two-line header is what stops a model's prose from being read as a file, and relaxing
it for one server's wire format would weaken the frame for every model.

Stdlib-only leaf. Text that carries no channel markup is returned unchanged, byte for byte, so
every model that already behaves is untouched and no per-model configuration can be set wrong.
"""

from __future__ import annotations

_TRANSCRIPT_OPENERS = ("<|channel|>", "<|start|>")
"""How a leaked transcript begins. Matched only at the start — see ``assistant_content``."""

_FINAL_MESSAGE = "<|channel|>final<|message|>"
"""Opens the one channel that is user-facing; ``analysis`` and ``commentary`` are not."""

_TERMINATORS = ("<|return|>", "<|end|>")
"""Close a harmony message. Grammar, not content — 8-10 bytes that are never the file."""


def assistant_content(text: str) -> str:
    """Return the assistant's user-facing content from ``text``.

    A reply is treated as a transcript only when it *opens* with channel markup. Reacting to the
    markers anywhere would misread a file that merely quotes them — and would empty it, since a
    payload discussing the format carries no final channel of its own.

    Args:
        text: One model reply, as assembled from the server's stream.

    Returns:
        The last final channel's content for a harmony transcript; ``text`` unchanged when it does
        not open as one; the empty string when it does but reaches no final channel, which the
        caller reads as a blocked task rather than offering reasoning to the file parser.
    """
    if not text.lstrip().startswith(_TRANSCRIPT_OPENERS):
        return text
    # The last final message, because a model that revises itself emits another one and the
    # earlier answer is the one it abandoned.
    _, separator, content = text.rpartition(_FINAL_MESSAGE)
    if not separator:
        return ""
    for terminator in _TERMINATORS:
        content, _, _ = content.partition(terminator)
    return content
