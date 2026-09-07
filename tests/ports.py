"""The two questions a test asks the OS about a TCP port: give me a free one, is this one bound.

Shared because both server-spawning suites ask the first, and asking it two different ways would
be two answers to one question. Neither helper knows anything about what a test is arranging — no
fixture, no double, no scenario — which is what makes a shared home free of the coupling that
makes shared test setup a bad trade.
"""

import socket

# A loopback connect either completes or is refused at once, so this bound is never reached in
# the answer's normal path — it exists so a port wedged half-open cannot hang the suite instead
# of failing it. Generous against scheduler noise, short against a test run's patience.
_PROBE_TIMEOUT_S = 0.5


def free_port() -> int:
    """Claim and release a port the OS says is free, then hand back its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_is_bound(port: int) -> bool:
    """Ask the OS whether anything is listening — the only honest teardown assertion."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(_PROBE_TIMEOUT_S)
        return probe.connect_ex(("127.0.0.1", port)) == 0
