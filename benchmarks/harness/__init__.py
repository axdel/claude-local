"""Public benchmark-harness surface outside the runtime ``claude_local`` package."""

from .case import BenchmarkCase
from .driver import (
    BenchmarkDriver,
    BenchmarkInterrupted,
    BenchmarkProgress,
    CaseResult,
    run_cases,
)
from .loader import load_case, load_cases
from .produced_code import write_produced_code
from .replay import replay_cases_http_client, replay_http_client
from .scorer import CaseScore, Scorecard, score_cases

__all__ = [
    "BenchmarkCase",
    "BenchmarkDriver",
    "BenchmarkInterrupted",
    "BenchmarkProgress",
    "CaseResult",
    "CaseScore",
    "Scorecard",
    "load_case",
    "load_cases",
    "replay_cases_http_client",
    "replay_http_client",
    "run_cases",
    "score_cases",
    "write_produced_code",
]
