"""Every shipped default base URL is the bare server root the backend contract expects.

``HttpxBackend`` owns the OpenAI path suffix (``/v1/chat/completions``), so a shipped default
that itself carried ``/v1`` would build ``.../v1/v1/chat/completions`` and 404 on the user's
first real run. Every replay transport in the loop and benchmark tests routes by request body,
not URL path, so the doubling never surfaces there — this is the one place that exercises real
URL construction against the exact values users run with.
"""

import httpx
import pytest

from benchmarks.run import _DEFAULT_BASE_URL as _BENCHMARK_DEFAULT
from claude_local.backend import HttpxBackend
from examples.quicksort.run import _DEFAULT_BASE_URL as _EXAMPLE_DEFAULT


@pytest.mark.parametrize(
    ("label", "base_url"),
    [("benchmark", _BENCHMARK_DEFAULT), ("example", _EXAMPLE_DEFAULT)],
)
def test_shipped_default_base_url_yields_one_versioned_openai_endpoint(
    label: str, base_url: str
) -> None:
    """The backend appends exactly one OpenAI version+endpoint segment to each shipped default.

    Oracle: the OpenAI chat-completions endpoint is ``/v1/chat/completions`` exactly once
    (a specification fact the backend appends), so a correct default is the bare root and the
    constructed URL carries the version segment once — never the doubled ``/v1/v1`` a
    ``/v1``-suffixed default would produce.
    """
    with httpx.Client() as client:
        backend = HttpxBackend(base_url, client, "model-under-test")

    assert "/v1/v1" not in backend._url, f"{label} default doubles the version prefix"
    assert backend._url.count("/v1") == 1  # exactly one version segment (a /v1 default → 2)
    assert backend._url.endswith("/v1/chat/completions")
