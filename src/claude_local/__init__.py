"""Claude Local — free local models as supervised, test-first code implementers.

A deterministic red-green loop that drives a local model to make a
frontier-authored failing test pass. The model receives a distilled rules card,
the task spec, and the failing test; it emits raw implementation text that the
loop applies to disk and runs. Design stage — see README.
"""

__version__ = "0.0.1"
