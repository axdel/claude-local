"""Tests for the prompt assembler (``claude_local.prompt``).

The stable prefix is the KV-cache reuse invariant (D-PROMPT-001): byte-identical across a task's
attempts so the server's prefill cache is reused. These tests pin ASSEMBLY DETERMINISM (same
spec -> identical bytes) and the absence of volatile tokens in the committed card + scaffolding —
never a server cache hit, which is not observable here. Feedback distillation is oracle-tested for
its declared byte cap and for preserving the failing node id after absolute-path stripping.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from factories import build_budget, build_context_file, build_task_spec
from hypothesis import given
from hypothesis import strategies as st

from claude_local.prompt import (
    FEEDBACK_BYTE_CAP,
    UNSCORABLE_REPLY_BYTE_CAP,
    PromptBuilder,
)
from claude_local.runner import TestScore
from claude_local.types import ContextFile, TaskSpec

# The committed static asset — the no-volatile-content test validates the real card, not a fixture.
REAL_CARD = Path(__file__).parents[1] / "src" / "claude_local" / "rules_card.md"
_REAL_CARD_BYTES = REAL_CARD.read_bytes()
_REAL_BUILDER = PromptBuilder(REAL_CARD)


def _spec(
    spec_text: str = "Implement add(a, b) returning a + b.",
    test_text: str = "def test_add():\n    assert add(1, 2) == 3\n",
    context_files: tuple[ContextFile, ...] = (),
) -> TaskSpec:
    return build_task_spec(
        impl_path="src/pkg/impl.py",
        spec_text=spec_text,
        test_text=test_text,
        budget=build_budget(max_attempts=4, generation_timeout_s=60.0),
        context_files=context_files,
    )


def _card(
    tmp_path: Path, content: str = "# Rules\nReturn the complete implementation file.\n"
) -> Path:
    # Created on demand so a caller can pass a fresh subdirectory per card, which is what a test
    # comparing two DIFFERENT cards needs — one directory would have the second overwrite the first
    tmp_path.mkdir(parents=True, exist_ok=True)
    card = tmp_path / "card.md"
    card.write_text(content, encoding="utf-8")
    return card


def test_card_digest_differs_when_the_card_content_differs(tmp_path: Path) -> None:
    """Two cards that would send different bytes to the model must not share a digest.

    This is the whole point of stamping it: a scorecard comparing two cards is only readable if
    the two runs are distinguishable, so a digest that collapsed distinct cards would silently
    label an A/B as a repeat of one arm.
    """
    first = PromptBuilder(_card(tmp_path / "a", "# Rules\nExtract the shared step.\n"))
    second = PromptBuilder(_card(tmp_path / "b", "# Rules\nInline the shared step.\n"))

    assert first.card_digest != second.card_digest


def test_card_digest_is_identical_for_identical_content_at_different_paths(
    tmp_path: Path,
) -> None:
    """The digest is over the BYTES, so the same card at two paths is the same card.

    ``implement`` takes a ``rules_card_path`` override, so a path-derived identity would report a
    card change whenever a file moved — and would report no change when a path was repointed at
    entirely different content, which is the failure that matters.
    """
    content = "# Rules\nReturn the complete implementation file.\n"
    at_one_path = PromptBuilder(_card(tmp_path / "first", content))
    at_another_path = PromptBuilder(_card(tmp_path / "second", content))

    assert at_one_path.card_digest == at_another_path.card_digest


def test_card_digest_ignores_trailing_newlines_the_prefix_strips(tmp_path: Path) -> None:
    """Cards differing only in trailing newlines produce identical prompts, so one digest.

    ``__init__`` rstrips the card before it is ever emitted, so those bytes reach no model. A
    digest taken before that strip would report two distinct cards for one prompt.
    """
    bare = PromptBuilder(_card(tmp_path / "bare", "# Rules\nDerive, do not restate.\n"))
    padded = PromptBuilder(_card(tmp_path / "padded", "# Rules\nDerive, do not restate.\n\n\n"))

    spec = _spec()
    assert bare.card_digest == padded.card_digest
    assert bare.stable_prefix(spec) == padded.stable_prefix(spec)


def _score(passed: int, failed: int, errors: int, collected: int, expected: int) -> TestScore:
    return TestScore(passed, failed, errors, collected, 0, expected)


# --- stable_prefix: determinism + completeness ------------------------------------


def test_stable_prefix_with_empty_context_matches_legacy_layout(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path, "# Rules\nKeep the prefix stable.\n"))

    assert builder.stable_prefix(_spec(spec_text="SPEC", test_text="TEST")) == (
        "# Rules\n"
        "Keep the prefix stable.\n\n"
        "## Implementation task\n"
        "Target file: src/pkg/impl.py\n\n"
        "SPEC\n\n"
        "## The test — immutable; do not modify or import it away. Make it pass.\n\n"
        "TEST\n"
    )


def test_stable_prefix_contains_card_spec_test_and_target(tmp_path: Path) -> None:
    # All three D-PROMPT-001 parts (card + spec + immutable test) plus the target file appear.
    builder = PromptBuilder(_card(tmp_path, "# CARD-SENTINEL\nrules.\n"))
    spec = _spec(spec_text="SPEC-SENTINEL body", test_text="TEST-SENTINEL body")
    prefix = builder.stable_prefix(spec)
    assert "CARD-SENTINEL" in prefix  # the static rules card
    assert "SPEC-SENTINEL" in prefix  # the task spec
    assert "TEST-SENTINEL" in prefix  # the immutable test
    assert spec.impl_path in prefix  # the target file name


def test_stable_prefix_renders_ordered_read_only_context_before_test(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path, "# Rules\nUse the existing interfaces.\n"))
    spec = _spec(
        spec_text="Implement the target.",
        test_text="def test_target():\n    assert target() == 3",
        context_files=(
            build_context_file(path="src/pkg/first.py", content="FIRST = 1"),
            build_context_file(path="src/pkg/second.py", content="SECOND = 2"),
        ),
    )

    assert builder.stable_prefix(spec) == (
        "# Rules\n"
        "Use the existing interfaces.\n\n"
        "## Implementation task\n"
        "Target file: src/pkg/impl.py\n\n"
        "Implement the target.\n\n"
        "## Existing files — read-only, integrate with them, "
        "do NOT reimplement or output them.\n\n"
        "### src/pkg/first.py\n\n"
        "FIRST = 1\n\n"
        "### src/pkg/second.py\n\n"
        "SECOND = 2\n\n"
        "## The test — immutable; do not modify or import it away. Make it pass.\n\n"
        "def test_target():\n"
        "    assert target() == 3\n"
    )


_CONTEXT_FILES = st.lists(
    st.builds(
        ContextFile,
        path=st.text(
            alphabet=st.characters(exclude_categories=("Cs",)),
            min_size=1,
            max_size=32,
        ).filter(lambda path: bool(path.strip())),
        content=st.text(
            alphabet=st.characters(exclude_categories=("Cs",)),
            max_size=128,
        ),
    ),
    max_size=8,
).map(tuple)


@given(context_files=_CONTEXT_FILES)
def test_stable_prefix_is_deterministic_and_rules_card_first(
    context_files: tuple[ContextFile, ...],
) -> None:
    spec = _spec(context_files=context_files)

    first = _REAL_BUILDER.stable_prefix(spec).encode()
    second = _REAL_BUILDER.stable_prefix(spec).encode()

    assert first == second
    assert first.startswith(_REAL_CARD_BYTES)


def test_stable_prefix_changes_when_spec_changes(tmp_path: Path) -> None:
    # Determinism is per-spec, not a constant blob: a different spec yields a different prefix.
    builder = PromptBuilder(_card(tmp_path))
    assert builder.stable_prefix(_spec(spec_text="A")) != builder.stable_prefix(
        _spec(spec_text="B")
    )


_TIMESTAMP_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),  # ISO date
    re.compile(r"\d{2}:\d{2}:\d{2}"),  # clock time
    re.compile(r"\b\d{10}\b"),  # unix timestamp
)
_FORBIDDEN_SUBSTRINGS = ("/private/", "/Users/", "/var/folders/", "TASK-", "run_id", "run-id")


def test_real_card_prefix_has_no_volatile_tokens() -> None:
    # The committed card + the builder's static scaffolding must carry NO dynamic content: a
    # timestamp/run-id/absolute path in the prefix silently discards the server prefill cache. A
    # CLEAN fixture spec is used, so any hit comes from the card or scaffolding, not provided text.
    builder = PromptBuilder(REAL_CARD)
    # The MAXIMAL prefix: both optional sections supplied, because each is appended only when it
    # is, so a minimal spec leaves the context-file and plan scaffolding entirely unscanned. A
    # stamp there passes every byte-identity assertion too, since one is constant within a run.
    prefix = builder.stable_prefix(
        _spec(
            spec_text="clean spec",
            test_text="clean test",
            context_files=(build_context_file(path="src/pkg/neighbor.py", content="clean ctx"),),
        ),
        plan="clean plan",
    )
    for pat in _TIMESTAMP_PATTERNS:
        assert pat.search(prefix) is None, f"volatile timestamp-like token: {pat.pattern}"
    for sub in _FORBIDDEN_SUBSTRINGS:
        assert sub not in prefix, f"forbidden volatile substring: {sub}"


# Every document that enumerates what the model is handed. Each carries its own copy of the
# composition — prose, an ASCII diagram, a bullet — and each is a place the list can go stale
# independently of the builder that actually assembles it.
_PREFIX_ENUMERATION_DOCUMENTS = (
    "docs/how-the-loop-works.html",
    "README.md",
    "CLAUDE.md",
    "skills/claude-local/SKILL.md",
)


def test_every_document_enumerating_the_prefix_names_each_section_it_can_carry() -> None:
    """No published enumeration of the prefix may fall behind the builder that assembles it.

    Oracle: the section set is read out of ``stable_prefix``'s own source rather than listed
    here — every ``_<NAME>_HEADER`` the method references is a section it can emit, and the
    constant's stem is the word a reader scans for. A sixth section added to the builder
    therefore reddens this test until every document names it, which a hand-copied list here
    could not do.

    This is a differential test because the prose is the half that rots, and it rots in more
    than one place at a time: plan-first added a fifth section and *four* documents kept
    describing four parts. Guarding only the walkthrough fixed one site of a class, so the
    document list above is the class. The rules card is the prefix's remaining member and is
    deliberately not covered here; it is pinned instead by the card-digest chain (D-CARD-003),
    which a prose edit cannot move.

    The stem is matched on word boundaries, not as a substring: ``"explanation"`` contains
    ``"plan"``, so a substring test would have gone green on a document that never mentions the
    plan section at all.

    Known weakness, measured rather than supposed: this scans each document *whole*, so a stem
    already present in another sense satisfies it while the enumeration stays stale. Widening
    this test caught CLAUDE.md and SKILL.md and left README.md green — its diagram still listed
    four parts, but ``plan`` appeared twice elsewhere naming the orchestrator's
    plan-then-author-oracle recipe, an unrelated homonym. That site was fixed by reading it, not
    by this assertion. So it is a ratchet against a section whose stem is *new* to a document,
    which is the failure that actually happened; it is not a proof that any enumeration is
    complete. Scoping it tighter would need a marker in each document, and the README's
    enumeration sits inside a fenced code block where an HTML comment would render as literal
    text — so the honest limitation is cheaper than a fragile anchor.
    """
    prefix_source = inspect.getsource(PromptBuilder.stable_prefix)
    sections = {name.lower() for name in re.findall(r"_([A-Z]+)_HEADER", prefix_source)}
    assert sections, "no prefix section constants found — the extraction itself is broken"

    root = Path(__file__).parents[1]
    stale: list[str] = []
    for relative_path in _PREFIX_ENUMERATION_DOCUMENTS:
        document = (root / relative_path).read_text(encoding="utf-8")
        unmentioned = sorted(
            s for s in sections if not re.search(rf"\b{re.escape(s)}\b", document, re.IGNORECASE)
        )
        if unmentioned:
            stale.append(f"{relative_path} never names {unmentioned}")
    assert not stale, "prefix section(s) missing from: " + "; ".join(stale)


# The card is a fixed cost paid on every attempt of every task, so "token-budgeted" has to be a
# bound something can fail against rather than a description. The ceiling is set from measurement:
# the 34,030-character doctrine card inflated the prefill 2.1-5.0x and lost on completion tokens
# for six of the eight models benchmarked, while the 4,714-character card that replaced it won.
# 8,000 leaves ~70% headroom for real growth and refuses anything approaching the card that lost.
_CARD_CHARACTER_CEILING = 8_000


def test_the_real_card_stays_within_its_stated_budget() -> None:
    """The card documents itself as token-budgeted; this is the budget.

    Oracle: the ceiling above, derived from the benchmark, not from measuring the current card —
    a test asserting the card's own length would pass at any size and pin nothing. It fails only
    when the card grows past a size already measured to cost more than it returns.
    """
    card = REAL_CARD.read_text(encoding="utf-8").rstrip("\n")

    assert len(card) <= _CARD_CHARACTER_CEILING, (
        f"rules card is {len(card)} characters, over the {_CARD_CHARACTER_CEILING} ceiling; "
        "every task pays this on every attempt"
    )


# --- distill_feedback: node id, path-strip, byte cap, score -----------------------


def test_distill_feedback_includes_the_failing_node_id(tmp_path: Path) -> None:
    builder = PromptBuilder(_card(tmp_path))
    raw = (
        "=== short test summary info ===\n"
        "FAILED tests/test_add.py::test_add_specific_case - AssertionError: assert 2 == 3\n"
        "=== 1 failed in 0.01s ===\n"
    )
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert "test_add_specific_case" in out


def test_distill_feedback_shows_the_source_that_produced_the_failure(tmp_path: Path) -> None:
    """The repair brief carries the file under repair, because the card asks for it by name.

    Oracle: the committed rules card instructs the model to "return the corrected complete file"
    and to "keep what already passed" — an instruction that names an artifact. A brief omitting it
    asks the model to correct something it has never seen and to preserve passing code it cannot
    read, so carrying the source is what the card already published, not an addition to it. It also
    makes the diagnostics already sent legible: a traceback naming a line is only actionable beside
    the file that line is in.
    """
    builder = PromptBuilder(_card(tmp_path))

    out = builder.distill_feedback(
        _score(0, 1, 0, 1, 1),
        "FAILED tests/t.py::test_add - AssertionError: assert -1 == 3\n",
        previous_attempt_source="def add(a, b):\n    return a - b\n",
    )

    assert "return a - b" in out


def test_distill_feedback_without_a_previous_attempt_carries_no_source_section(
    tmp_path: Path,
) -> None:
    """The first attempt has produced nothing, so the brief must not announce a file.

    Oracle: the loop calls this only after a scored attempt, but the default has to hold on its
    own — an empty source rendered under its header would tell the model its previous attempt was
    an empty file, which is a false statement about its own work and exactly the kind of thing a
    model will dutifully try to reconcile.
    """
    builder = PromptBuilder(_card(tmp_path))

    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), "FAILED tests/t.py::test_add - boom\n")

    assert "previous attempt" not in out.lower()


def test_a_large_previous_attempt_cannot_evict_the_failure_diagnostics(tmp_path: Path) -> None:
    """Source and failure are capped independently, so neither can starve the other.

    Oracle: the two sections answer different questions — what you wrote, and how it broke — and a
    repair needs both. Under one shared cap the larger section decides how much of the smaller one
    survives, so a big implementation would silently buy itself less diagnosis exactly when it
    needs more. Independent caps make each section's budget a property of that section alone.
    """
    builder = PromptBuilder(_card(tmp_path))
    node_id = "FAILED tests/t.py::test_add - AssertionError: assert -1 == 3"

    out = builder.distill_feedback(
        _score(0, 1, 0, 1, 1),
        f"{node_id}\n",
        previous_attempt_source="# filler\n" * 4000,  # far over any single-section cap
    )

    assert node_id in out
    assert "# filler" in out  # the source is still shown, just trimmed


def test_the_reframe_quotes_the_reply_before_it_corrects_it(tmp_path: Path) -> None:
    """The correction leads with the model's own words, then says no file exists.

    Oracle: this is the nudge contract — counterevidence first, imperative last — applied to a
    reply that produced nothing to score. The evidence has to be the raw reply because that is the
    only artifact there is: no file was written, so there is no source and no run output to show.
    An instruction alone leaves the model where it already was, having read the rules once and
    answered this way regardless.
    """
    builder = PromptBuilder(_card(tmp_path))
    reply = (
        "Let me read the files.\n<tool_call><function=Read>app/schemas.py</function></tool_call>"
    )

    reframe = builder.reframe_for(reply)

    assert reply in reframe
    assert reframe.index(reply) < reframe.index("no file")  # evidence leads, imperative follows


def test_a_long_unscorable_reply_cannot_push_the_correction_out_of_reach(tmp_path: Path) -> None:
    """The quoted reply is capped, so the imperative survives however much the model wrote.

    Oracle: the reply is unbounded — a model can answer with a page of reasoning — while the
    correction is the one part that has to be read. An uncapped quote would let the evidence
    displace the instruction that gives it a point, which is the same reason every other section
    of a brief is capped separately.
    """
    builder = PromptBuilder(_card(tmp_path))

    reframe = builder.reframe_for("x" * (UNSCORABLE_REPLY_BYTE_CAP * 4))

    assert len(reframe.encode()) < UNSCORABLE_REPLY_BYTE_CAP * 4
    assert reframe.rstrip().endswith("around it.")  # the imperative is still the last thing read


def test_the_nudge_ladder_escalates_and_is_finite(tmp_path: Path) -> None:
    """Each rung asks a different question, and the ladder ends rather than repeating.

    Oracle: a nudge exists only to make the next prompt differ from the one that produced a stall,
    so two rungs with the same text would be no perturbation at all — the second would reproduce
    the first's absorbing state exactly. And the ladder has to end: past the last rung there is no
    further question this card knows how to ask, and continuing would spend the remaining budget
    re-asking one the model has already answered three ways.
    """
    builder = PromptBuilder(_card(tmp_path))

    first = builder.nudge_for(1)
    second = builder.nudge_for(2)

    assert first and second
    assert first != second
    assert builder.nudge_for(99) is None


def test_no_rung_tells_the_model_it_sent_an_identical_file(tmp_path: Path) -> None:
    """A rung answers either shape of stall, so it may not assert the shape it did not see.

    Oracle: the loop escalates on a verbatim repeat AND on a plateau — different code that scored
    no better. A rung claiming the file came back identical is therefore false half the time, and
    it is false about the model's own last output, which the model can check. A weak model handed
    a false premise argues with it instead of repairing the code, so this is a correctness bound on
    the prompt, not a wording preference.

    Universally quantified over the closed rung set rather than the two rungs that exist today: the
    ladder is expected to grow, and a third rung reintroducing the claim is exactly the regression
    worth catching. The forbidden terms come from the two shapes' definitions, never from reading
    the current text — each names sameness of the FILE, which only a repeat guarantees.
    """
    builder = PromptBuilder(_card(tmp_path))
    forbidden = (
        "identical",
        "the same code",
        "same file",
        "repeated yourself",
        "sending it again",
    )

    rungs = []
    rung = builder.nudge_for(len(rungs) + 1)
    while rung is not None:
        rungs.append(rung)
        rung = builder.nudge_for(len(rungs) + 1)

    assert rungs  # a vacuous pass over an empty ladder would prove nothing
    for index, text in enumerate(rungs, start=1):
        lowered = text.lower()
        for claim in forbidden:
            assert claim not in lowered, f"rung {index} asserts a repeat-only fact: {claim!r}"


def test_a_nudge_reaches_the_end_of_the_repair_brief(tmp_path: Path) -> None:
    """The nudge is the instruction for the next attempt, so it sits closest to generation.

    Oracle: everything else in the brief is context — the file, the failure. The nudge is the only
    imperative about what to do differently, and it is what has to change the prompt's bytes, so
    burying it above several kilobytes of source would waste the one section that exists to be
    read. It is also short and fixed-size, so placing it after the capped sections cannot push
    anything out.
    """
    builder = PromptBuilder(_card(tmp_path))
    nudge = builder.nudge_for(1)
    assert nudge is not None

    out = builder.distill_feedback(
        _score(0, 1, 0, 1, 1),
        "FAILED tests/t.py::test_add - boom\n",
        previous_attempt_source="def add(a, b):\n    return a - b\n",
        nudge=nudge,
    )

    assert out.endswith(nudge)


# Captured verbatim from `pytest -q` on a two-failure module, not authored from memory: an
# assertion failure whose `>` line is in the TEST, and a raised exception whose `>` line is in the
# IMPLEMENTATION. Boundary fixtures are only evidence when they are the real wire bytes.
_REAL_PYTEST_FAILURES = """\
=================================== FAILURES ===================================
_____________________________ test_add_two_and_two _____________________________

    def test_add_two_and_two():
>       assert add(2, 2) == 4
E       assert 0 == 4
E        +  where 0 = add(2, 2)

test_impl.py:5: AssertionError
___________________________ test_add_raises_on_text ____________________________

    def test_add_raises_on_text():
>       add("a", 1)

test_impl.py:9:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

a = 'a', b = 1

    def add(a, b):
>       return a - b
               ^^^^^
E       TypeError: unsupported operand type(s) for -: 'str' and 'int'

impl.py:2: TypeError
=========================== short test summary info ============================
FAILED test_impl.py::test_add_two_and_two - assert 0 == 4
FAILED test_impl.py::test_add_raises_on_text - TypeError: unsupported operand...
2 failed in 0.01s
"""

# The escalation section, sliced at its own header rather than by a byte offset back from the
# nudge. The distinction matters: the failure brief ABOVE the escalation also quotes failures, so a
# loose window would let these tests pass on the brief's content while the injection was broken.
_ESCALATION_ANCHOR = "verbatim from the run"


def _escalation_of(brief: str) -> str:
    """The part of a repair brief from the counterevidence header onward, or empty if absent."""
    return "" if _ESCALATION_ANCHOR not in brief else brief[brief.index(_ESCALATION_ANCHOR) :]


def test_a_nudge_is_led_by_the_executed_counterevidence_not_by_exhortation(tmp_path: Path) -> None:
    """The escalation quotes what the code actually did, not a request to go find out.

    Oracle: pytest's own contract, read off the captured bytes above — it marks the statement that
    failed with ``>`` and the concrete result with ``E``, so ``assert 0 == 4`` is the expected
    value here because that is what the run printed — derived independently of anything this
    module computes. The design reason is the controlled finding this ladder is built on: on
    frozen small code models, executed counterevidence carries the repair signal while generic
    retry instructions carry almost none. A rung that only exhorts is the arm found inert.
    """
    builder = PromptBuilder(_card(tmp_path))
    nudge = builder.nudge_for(1)
    assert nudge is not None

    out = builder.distill_feedback(_score(0, 2, 0, 2, 2), _REAL_PYTEST_FAILURES, nudge=nudge)
    escalation = _escalation_of(out)

    assert "assert 0 == 4" in escalation
    assert "assert add(2, 2) == 4" in escalation


def test_only_the_first_failures_evidence_leads_the_nudge(tmp_path: Path) -> None:
    """One failure is quoted, because the rung it introduces asks for one path to change.

    Oracle: the captured run holds two independent failures — an assertion and a ``TypeError``. The
    rung says "make that path produce the value the test expects", which is a single-target
    instruction; leading it with every failure at once contradicts it and asks a model that has
    already repeated itself once to fix everything simultaneously. The second failure stays
    reachable in the diagnostics above, so nothing is hidden — it is only not what the imperative
    points at.

    Both halves are asserted. "The second failure is absent" is vacuously true of an escalation
    that quoted nothing, so on its own it would pass against the very defect it exists to catch.
    """
    builder = PromptBuilder(_card(tmp_path))
    nudge = builder.nudge_for(1)
    assert nudge is not None

    out = builder.distill_feedback(_score(0, 2, 0, 2, 2), _REAL_PYTEST_FAILURES, nudge=nudge)
    escalation = _escalation_of(out)

    assert "assert 0 == 4" in escalation  # the first failure IS quoted
    assert "TypeError: unsupported operand" not in escalation  # the second is NOT


def test_a_run_with_no_marked_failure_falls_back_to_the_bare_rung(tmp_path: Path) -> None:
    """No failing statement to quote means no counterevidence header — never an empty one.

    Oracle: a collection error or an import failure aborts before any test body runs, so pytest
    emits no ``>``/``E`` pair at all. Rendering the header over nothing would assert that a failing
    statement was found and shown, which is false, and a model reconciles what the prompt claims —
    the same reason an absent previous attempt renders no source section.
    """
    builder = PromptBuilder(_card(tmp_path))
    nudge = builder.nudge_for(1)
    assert nudge is not None

    out = builder.distill_feedback(
        _score(0, 1, 1, 0, 1),
        "ERROR tests/t.py - ImportError: no module named 'app'\n",
        nudge=nudge,
    )

    assert out.endswith(nudge)
    assert _escalation_of(out) == ""


def test_distill_feedback_strips_absolute_paths(tmp_path: Path) -> None:
    # The volatile worktree/tmp prefix is stripped; the useful relative tail (file:line) is kept.
    builder = PromptBuilder(_card(tmp_path))
    raw = "/private/var/folders/ab/xy/worktree/test_oracle.py:12: AssertionError\n"
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert "/private/var/folders" not in out
    assert "test_oracle.py:12" in out


def test_distill_feedback_strips_the_run_duration_so_one_failure_distills_identically(
    tmp_path: Path,
) -> None:
    """Two runs of one unchanged failing oracle must distill to one tail, or the loop is noisy.

    Oracle: measured, not supposed — two identical ``pytest`` invocations over one unchanged file
    produced byte-identical output but for ``1 failed in 0.15s`` against ``1 failed in 0.13s``.
    That is pytest's LAST line, so it is always inside the 40-line tail, and the tail is the only
    part of the prompt that changes between attempts. An unstripped duration therefore makes an
    identical failure ask a different question every run, which both defeats verbatim-repeat
    detection (D-LOOP-004) and leaves the benchmark unable to tell a better model from noise.
    """
    builder = PromptBuilder(_card(tmp_path))
    summary = (
        "=== short test summary info ===\n"
        "FAILED test_oracle.py::test_health - AssertionError: assert 'down' == 'ok'\n"
    )
    faster = builder.distill_feedback(
        _score(0, 1, 0, 1, 1), f"{summary}=== 1 failed in 0.13s ===\n"
    )
    slower = builder.distill_feedback(
        _score(0, 1, 0, 1, 1), f"{summary}=== 1 failed in 0.15s ===\n"
    )

    assert faster == slower
    # Equal because the volatile field is gone, not because the whole tail was thrown away.
    assert "0.13s" not in faster
    assert "1 failed" in faster
    assert "test_health" in faster


def test_distill_feedback_keeps_a_duration_that_is_the_failure_itself(tmp_path: Path) -> None:
    """Only pytest's own run-time footer is volatile; a duration the test ASSERTED is evidence.

    Oracle: a timeout or latency assertion prints its measured seconds inside the traceback, and
    that number is exactly what the model must read to fix the code. Stripping every ``N.NNs`` in
    the output would delete the diagnosis along with the noise, so the strip is anchored to the
    footer's ``=== … in N.NNs ===`` shape rather than applied to any number it can find.
    """
    builder = PromptBuilder(_card(tmp_path))
    raw = (
        "E       assert 2.51 == approx(0.5)\n"
        "test_oracle.py:9: took 2.51s, budget was 0.50s\n"
        "=== 1 failed in 2.90s ===\n"
    )

    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)

    assert "took 2.51s" in out  # the asserted measurement survives
    assert "in 2.90s" not in out  # the footer's own run time does not


def test_distill_feedback_normalizes_the_address_in_a_default_object_repr(tmp_path: Path) -> None:
    """An identity failure must read the same on two runs, though the objects moved in memory.

    Oracle: ``object.__repr__`` embeds ``id()``, which under ASLR differs every process — measured
    at ``0x10b19c590`` against ``0x1088d0590`` on two runs of ONE unchanged implementation. Unlike
    the tmpdir name and pytest's header, this one has no fix at the source: the address IS the
    default repr. So it is normalized here, keeping the type name — which is the diagnosis — and
    dropping only the address, which says nothing about the code (D-PROMPT-002).
    """
    builder = PromptBuilder(_card(tmp_path))
    template = "E       assert <app.main.App object at {0}> is not <app.main.App object at {0}>\n"

    first = builder.distill_feedback(_score(0, 1, 0, 1, 1), template.format("0x10b19c590"))
    second = builder.distill_feedback(_score(0, 1, 0, 1, 1), template.format("0x1088d0590"))

    assert first == second
    assert "0x10b19c590" not in first
    assert "app.main.App object at" in first  # the type survives; only the address is dropped


def test_distill_feedback_caps_bytes_and_keeps_node_id(tmp_path: Path) -> None:
    # 60 fat lines (300 chars each) make the tail alone far exceed the 4 KiB cap, forcing real
    # truncation — yet the node id (placed ahead of the tail) survives the trim from the end.
    builder = PromptBuilder(_card(tmp_path))
    fat = "\n".join("x" * 300 for _ in range(60))
    raw = f"FAILED tests/test_add.py::test_pinned_node - AssertionError\n{fat}\n=== 1 failed ===\n"
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), raw)
    assert len(out.encode("utf-8")) <= FEEDBACK_BYTE_CAP  # hard cap honored (declared contract)
    assert "[...truncated]" in out  # truncation actually fired (the tail overflowed the cap)
    assert (
        "test_pinned_node" in out
    )  # node id survives capping — it sits ahead of the trimmed tail


def test_distill_feedback_reports_the_score_counts(tmp_path: Path) -> None:
    # Hand-derived: 2 of 3 passed, 1 failed -> both counts appear so the model knows the gap.
    builder = PromptBuilder(_card(tmp_path))
    out = builder.distill_feedback(_score(2, 1, 0, 3, 3), "some output\n")
    assert "2/3 passed" in out
    assert "1 failed" in out


def test_distill_feedback_short_output_is_not_truncated(tmp_path: Path) -> None:
    # Capping fires only when over the cap: a small blob keeps its content, no truncation marker.
    builder = PromptBuilder(_card(tmp_path))
    out = builder.distill_feedback(_score(0, 1, 0, 1, 1), "FAILED tests/t.py::test_x - boom\n")
    assert "[...truncated]" not in out
