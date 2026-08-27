"""The local economy record — the LOCAL half of the per-task economy measurement.

claude-local writes only the local half; the orchestrator half and the shared correlation keys are
owned by claude-protocol (D-TELEMETRY-001). ``LocalEconomyRecord`` is that half: which model ran,
how many logical calls and loop attempts it took, how many completion tokens it decoded over how
many model-seconds, the mean decode rate, whether any count was estimated, and the final status.
The telemetry module is its single writer (RESOURCE_OWNERSHIP).

``from_run`` AGGREGATES a timeline of per-attempt ``GenerationResult`` (client token usage
and timing) into those scalars. Two counts are deliberately NOT derived from the timeline.
``total_calls`` is the client's own logical-call ledger — it increments at entry, so it
survives a call that raised mid-stream and left the timeline one result short — while
``attempts`` is the loop's cycle count. Both are passed in, because ``len(timeline)`` is
neither. ``mean_tokens_per_second`` is a guarded quotient (completion tokens only — it
excludes the cached prompt prefix) that is ``None``, never a crash, when no model-seconds
elapsed. ``tokens_estimated`` rises to True if any attempt fell back to the char proxy, so
a reader never mistakes an estimate for a server-counted total.

Cold path by construction: aggregation and the JSON write run once at loop exit, off the inference
hot path, so this module favours plain sums and ``json.dumps`` over anything built for speed (E6).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from claude_local.client import GenerationResult
    from claude_local.types import Status

# Model ids carry '/' and ':' (``mlx-community/Qwen2.5-Coder-7B``); collapse every run of unsafe
# filename characters to a single '-' so the record lands as one flat file, not a subtree.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class LocalEconomyRecord:
    """The local half of one task's economy record — aggregated once, written once, never mutated.

    ``total_calls`` (client logical calls) and ``attempts`` (loop cycles) are distinct counts, both
    supplied by the caller; the token and timing totals are aggregated from the run's timeline.
    ``mean_tokens_per_second`` is ``None`` when no model-seconds elapsed.
    """

    model: str
    total_calls: int
    total_completion_tokens: int
    total_model_seconds: float
    mean_tokens_per_second: float | None
    tokens_estimated: bool
    status: Status
    attempts: int

    @classmethod
    def from_run(
        cls,
        *,
        model: str,
        results: Sequence[GenerationResult],
        total_calls: int,
        attempts: int,
        status: Status,
    ) -> LocalEconomyRecord:
        """Aggregate a run's per-attempt timeline into the local economy record.

        ``total_calls`` and ``attempts`` are carried through verbatim — never taken as
        ``len(results)``, which undercounts when a call raised before yielding a result.
        """
        total_completion_tokens = sum(r.completion_tokens for r in results)
        total_model_seconds = sum((r.seconds for r in results), 0.0)
        mean = total_completion_tokens / total_model_seconds if total_model_seconds > 0 else None
        return cls(
            model=model,
            total_calls=total_calls,
            total_completion_tokens=total_completion_tokens,
            total_model_seconds=total_model_seconds,
            mean_tokens_per_second=mean,
            tokens_estimated=any(r.tokens_estimated for r in results),
            status=status,
            attempts=attempts,
        )

    def write(self, directory: Path) -> Path:
        """Serialize the record as JSON into ``directory`` (created if absent); return the path.

        The filename is the slugged model plus a millisecond timestamp, so concurrent tasks never
        clobber one another. Returns where it wrote — the caller need not predict the name.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._slug()}-{int(time.time() * 1000)}.json"
        path.write_text(json.dumps(self._as_dict(), indent=2), encoding="utf-8")
        return path

    def _slug(self) -> str:
        """The model id reduced to a flat, filesystem-safe filename stem."""
        return _UNSAFE_FILENAME_CHARS.sub("-", self.model).strip("-")

    def _as_dict(self) -> dict[str, object]:
        """JSON-ready mapping of the record; ``status`` becomes its lowercase value."""
        return {
            "model": self.model,
            "total_calls": self.total_calls,
            "total_completion_tokens": self.total_completion_tokens,
            "total_model_seconds": self.total_model_seconds,
            "mean_tokens_per_second": self.mean_tokens_per_second,
            "tokens_estimated": self.tokens_estimated,
            "status": self.status.value,
            "attempts": self.attempts,
        }
