"""Reading the assistant's reply out of a harmony channel transcript.

The channel markup in every fixture is a **capture**, recorded from mlx_vlm serving gpt-oss-20b
on 2026-09-05 and pasted verbatim — never a guess at the wire. That provenance is the whole point:
markup invented from a mental model of the format would agree with a parser written from the same
model and prove nothing about what the server actually sends. Where a fixture's *payload* is
synthetic rather than captured, the comment above it says so and why.

The oracle is OpenAI's published harmony grammar, not this parser: a message is opened by
``<|channel|><name><|message|>``, the model's chain-of-thought goes to ``analysis``, and the
user-facing answer to ``final``.
"""

from __future__ import annotations

from claude_local.edits import extract_file
from claude_local.harmony import assistant_content

# Captured verbatim: curl -X POST .../v1/chat/completions -d '{"messages":[{"role":"user",
# "content":"Reply with exactly: OK"}],"enable_thinking":false}' -> choices[0].message.content
_CAPTURED_SHORT_REPLY = (
    '<|channel|>analysis<|message|>The user says: "Reply with exactly: OK". So we must reply '
    'with exactly "OK". No extra spaces, no quotes. Just OK.<|end|><|start|>assistant'
    "<|channel|>final<|message|>OK"
)

# The same server driving the bundled quicksort task wrapped its answer in this exact markup.
# Split provenance, stated because it matters: the channel structure is captured verbatim, while
# the payload is synthetic. Transcribing the real 523-byte payload out of terminal output landed
# 16 bytes adrift — the same arithmetic slip that later retired the declared byte count from the
# frame itself (D-EDITS-002), measured here first on a human rather than the model.
_CODE = "def quicksort(values: list[int]) -> list[int]:\n    return sorted(values)\n"
_WHOLE_FILE_REPLY_IN_CAPTURED_MARKUP = (
    "<|channel|>analysis<|message|>We need to implement quicksort in src/quicksort.py. "
    "Let's produce the file.<|end|><|start|>assistant<|channel|>final<|message|>"
    f"FILE: src/quicksort.py\n\n{_CODE}"
)


def test_the_final_channel_is_returned_without_the_reasoning_that_preceded_it() -> None:
    """Oracle: harmony routes chain-of-thought to `analysis` and the answer to `final`.

    OpenAI's own guidance is that the analysis channel is not user-facing, so the assistant's
    reply is the final channel's content and nothing else.
    """
    assert assistant_content(_CAPTURED_SHORT_REPLY) == "OK"


def test_a_captured_reply_becomes_parseable_as_a_whole_file_frame() -> None:
    """The end-to-end property, on the real capture: markup in, a usable frame out.

    Oracle: `extract_file` requires a single `FILE: ` line to be the entire text before the blank
    line, so channel markup ahead of it disqualifies the frame. This is the differential form of
    the bug — the same bytes fail before the channel content is separated and succeed after, so it
    pins the defect rather than the parser's shape.
    """
    assert (
        extract_file(_WHOLE_FILE_REPLY_IN_CAPTURED_MARKUP) is None
    )  # the raw transcript is unusable

    reply = extract_file(assistant_content(_WHOLE_FILE_REPLY_IN_CAPTURED_MARKUP))

    assert reply is not None
    assert reply.path == "src/quicksort.py"
    assert reply.payload == _CODE.encode()


def test_text_without_channel_markup_is_returned_byte_identically() -> None:
    """Every other model in the registry must be untouched by this.

    Oracle: the frame's payload runs to the end of the reply (D-EDITS-002), so trailing whitespace
    IS the file's last bytes for models that never emit channels — dropping it would silently
    rewrite their source. Identity is the requirement, not merely "still parses".
    """
    plain = "FILE: src/thing.py\n\nprint(1)\n\n\n"

    assert assistant_content(plain) == plain


def test_a_payload_that_merely_mentions_the_markup_is_returned_byte_identically() -> None:
    """A file whose content discusses harmony must survive untouched.

    Oracle: a transcript is identified by how the reply OPENS, because a server emitting one puts
    the channel header first. Treating the marker as significant anywhere would let a model
    writing a parser for this very format have its file silently replaced by a fragment of
    itself — and, worse, emptied when no final channel follows.
    """
    payload = 'FILE: src/harmony.py\n\nMARKER = "<|channel|>"  # opens a harmony message\n'

    assert assistant_content(payload) == payload


def test_a_transcript_that_never_reaches_the_final_channel_yields_no_content() -> None:
    """Reasoning alone is not an answer, and must not be offered to the file parser.

    Oracle: the model spent its budget without emitting a user-facing message, so there is no
    assistant content. Returning the raw reasoning instead would let prose be read as a file
    payload — the exact confusion the strict frame exists to prevent. Empty resolves to BLOCKED,
    which is the honest outcome.
    """
    truncated = "<|channel|>analysis<|message|>Let me think about the pivot choice at length"

    assert assistant_content(truncated) == ""


def test_the_last_final_channel_wins_when_a_transcript_carries_several() -> None:
    """Oracle: harmony is a message sequence, so a later message supersedes an earlier one.

    A model that revises itself emits a second final message; taking the first would return the
    answer it abandoned.
    """
    revised = (
        "<|channel|>final<|message|>first answer<|end|>"
        "<|start|>assistant<|channel|>final<|message|>second answer"
    )

    assert assistant_content(revised) == "second answer"


def test_a_terminator_after_the_final_message_is_not_part_of_the_content() -> None:
    """Oracle: `<|return|>` and `<|end|>` close a harmony message; they are grammar, not text.

    Leaving one attached would append 10 bytes to a byte-counted payload and fail the count.
    """
    assert assistant_content("<|channel|>final<|message|>answer<|return|>") == "answer"
    assert assistant_content("<|channel|>final<|message|>answer<|end|>") == "answer"
