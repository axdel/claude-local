"""Resolution tests for the model registry.

The registry is the curated table of models that *may* be served; the model store is the
directory of weights actually on disk. These are two concepts, and the tests hold them apart:
a name absent from the registry and a name present in the registry but missing from disk are
different failures, because they call for different fixes (add a row vs pull the weights).

Expected values are read off the registry format the fixtures declare, never off the parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_local.model_registry import (
    MalformedRegistry,
    ModelNotPresent,
    ModelRegistry,
    UnknownModel,
    UnservableCombination,
    is_unservable_combination,
)

_REGISTRY_FIXTURE = "\n".join(
    (
        "# A comment line, skipped.",
        "#",
        "NAME|REPO|DRAFT|PORT|SIZE|FLAGS|PARAMS|NOTE",
        "gpt-oss-20b|mlx-community/gpt-oss-20b-Q8|-|8088|12.1G|-|-|Fast MoE.",
        # Joined rather than written as one block so this row keeps realistic repo ids and a
        # two-parameter PARAMS cell without running past the line limit. The eight columns are
        # what the format declares, so a row of them is simply long.
        "Qwen3.8-27B|lmstudio/Qwen3.8-27B-6bit|lukaskremla/Qwen3.8-MTP|8080|22.8G"
        "|--enable-thinking|enable_thinking=false top_k=20|Dense.",
        "Gemma4-31B|lmstudio/gemma-4-31B-6bit|-|8082|26.1G|--enable-thinking --kv-bits 8|-|Slow.",
        "",
    )
)


def _registry(tmp_path: Path, *, present: tuple[str, ...] = ()) -> ModelRegistry:
    """Build a registry over a fixture table and a store holding ``present`` models."""
    registry_path = tmp_path / "models.psv"
    registry_path.write_text(_REGISTRY_FIXTURE)
    store_root = tmp_path / "store"
    store_root.mkdir()
    for name in present:
        (store_root / name).mkdir()
    return ModelRegistry(registry_path=registry_path, store_root=store_root)


def test_resolve_returns_the_registry_row_and_the_store_path(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("gpt-oss-20b",)).resolve("gpt-oss-20b")

    # Oracle: the fixture's own row declares each value; nothing here was read off the parser.
    assert resolved.repo == "mlx-community/gpt-oss-20b-Q8"
    assert resolved.port == 8088
    assert resolved.draft_repo is None  # a "-" in the DRAFT column means no draft model
    assert resolved.path == tmp_path / "store" / "gpt-oss-20b"


def test_resolve_carries_the_draft_model_when_the_row_names_one(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("Qwen3.8-27B",)).resolve("Qwen3.8-27B")

    assert resolved.draft_repo == "lukaskremla/Qwen3.8-MTP"


def test_generation_params_resolve_to_typed_values_not_strings(tmp_path: Path) -> None:
    """A parameter reaches the request body as the type the server's schema declares.

    Oracle: the column is read as ``key=value`` pairs and the values are JSON scalars, so
    ``enable_thinking=false`` is the boolean ``False`` and ``top_k=20`` the integer ``20`` — the
    types ``mlx_vlm``'s request schema declares for those fields. The string ``"false"`` would be
    truthy in the body and silently leave thinking on, which is the exact defect this column
    exists to fix, so the distinction is the whole test rather than a detail of it.
    """
    resolved = _registry(tmp_path, present=("Qwen3.8-27B",)).resolve("Qwen3.8-27B")

    assert resolved.generation_params == {"enable_thinking": False, "top_k": 20}


def test_an_absent_params_column_declares_no_generation_params(tmp_path: Path) -> None:
    """A "-" means nothing to declare, matching every other optional column in the format.

    Oracle: ``-`` already means "nothing to declare" for DRAFT and FLAGS (``_ABSENT``), so one
    reading of that token across the format is what keeps the registry legible. An empty mapping —
    not ``None`` — because the consumer forwards it into a request body either way.
    """
    resolved = _registry(tmp_path, present=("gpt-oss-20b",)).resolve("gpt-oss-20b")

    assert resolved.generation_params == {}


def test_a_generation_parameter_without_a_value_is_refused(tmp_path: Path) -> None:
    """A bare token in the PARAMS column has no field to bind, so the row fails closed.

    Oracle: the column's declared format is ``key=value`` pairs, so a token carrying no ``=``
    names no request field. Reading it as a valueless flag would invent a shape the server's
    request schema has no slot for — and silently, since an unknown body field is simply ignored.
    The refusal names the offending token, which is what an author needs to repair the row.
    """
    registry_path = tmp_path / "models.psv"
    registry_path.write_text(
        "NAME|REPO|DRAFT|PORT|SIZE|FLAGS|PARAMS|NOTE\n"
        "bad-params|repo|-|8088|1G|-|enable_thinking|Missing the value.\n"
    )
    store_root = tmp_path / "store"
    (store_root / "bad-params").mkdir(parents=True)
    registry = ModelRegistry(registry_path=registry_path, store_root=store_root)

    with pytest.raises(MalformedRegistry) as refusal:
        registry.resolve("bad-params")

    assert "enable_thinking" in str(refusal.value)


def test_a_declared_draft_whose_weights_are_absent_resolves_to_no_draft_path(
    tmp_path: Path,
) -> None:
    """A draft named in the registry but never pulled must not read as servable.

    Oracle: the registry/store split — the DRAFT column says what may be pulled, only the store
    proves what is on disk. A consumer handed the repo id for absent weights downloads them, so
    the path staying None is what keeps that branch unreachable. This is the real shipped state:
    the registry names a draft for Qwen3.8-27B whose weights are not in the store.
    """
    resolved = _registry(tmp_path, present=("Qwen3.8-27B",)).resolve("Qwen3.8-27B")

    assert resolved.draft_repo == "lukaskremla/Qwen3.8-MTP"  # the row still records provenance
    assert resolved.draft_path is None


def test_a_draft_present_in_the_store_resolves_to_its_path(tmp_path: Path) -> None:
    """Oracle: a draft is stored beside the model it accelerates, under a '-MTP' suffix."""
    resolved = _registry(tmp_path, present=("Qwen3.8-27B", "Qwen3.8-27B-MTP")).resolve(
        "Qwen3.8-27B"
    )

    assert resolved.draft_path == tmp_path / "store" / "Qwen3.8-27B-MTP"


def test_a_model_with_no_declared_draft_has_no_draft_path_even_if_a_directory_exists(
    tmp_path: Path,
) -> None:
    """The row decides whether a draft exists at all; a stray directory must not enable one.

    Oracle: speculative decoding needs a draft that actually matches the model, which only the
    registry can assert. Inferring one from a directory name would serve an unvalidated pairing.
    """
    resolved = _registry(tmp_path, present=("gpt-oss-20b", "gpt-oss-20b-MTP")).resolve(
        "gpt-oss-20b"
    )

    assert resolved.draft_repo is None
    assert resolved.draft_path is None


def test_resolve_splits_serving_flags_into_separate_arguments(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("Gemma4-31B",)).resolve("Gemma4-31B")

    # Oracle: flags reach a process as an argv sequence, so the one-flag-per-element split is
    # the contract — a single joined string would be passed as one argument and rejected.
    assert resolved.flags == ("--enable-thinking", "--kv-bits", "8")


def test_a_row_with_no_flags_resolves_to_an_empty_sequence(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("gpt-oss-20b",)).resolve("gpt-oss-20b")

    assert resolved.flags == ()


def test_an_unknown_name_is_refused_and_names_what_is_available(tmp_path: Path) -> None:
    """A name absent from the registry cannot be served, and the refusal must be actionable."""
    registry = _registry(tmp_path, present=("gpt-oss-20b",))

    with pytest.raises(UnknownModel) as refusal:
        registry.resolve("Llama-99B")

    # Oracle: a bare "unknown model" leaves the caller guessing; the available names are the
    # only thing that makes the error actionable, so they are part of the contract.
    assert "Llama-99B" in str(refusal.value)
    assert "gpt-oss-20b" in str(refusal.value)


def test_a_registered_model_absent_from_the_store_is_a_distinct_refusal(tmp_path: Path) -> None:
    """Registered-but-not-pulled is not the same failure as unknown, and must not be conflated.

    Oracle: the registry lists what *may* be pulled, so a row proves nothing about the disk.
    The two cases call for different fixes — add a row vs pull the weights — so a caller that
    cannot tell them apart cannot act on either.
    """
    registry = _registry(tmp_path, present=())  # registered, but nothing on disk

    with pytest.raises(ModelNotPresent) as refusal:
        registry.resolve("gpt-oss-20b")

    assert "gpt-oss-20b" in str(refusal.value)


def test_names_lists_the_registry_skipping_comments_and_the_header(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    # Oracle: the fixture declares 3 model rows behind 2 comment lines and 1 header line.
    assert registry.names() == ("gpt-oss-20b", "Qwen3.8-27B", "Gemma4-31B")


def test_servable_names_keeps_only_the_rows_whose_weights_are_in_the_store(
    tmp_path: Path,
) -> None:
    """Oracle: 2 of the fixture's 3 rows have a store directory, so exactly those 2 are servable.

    The middle row is the one left unpulled, so a filter that returned a prefix or a suffix of the
    registry — rather than the actually-present subset — would not survive this arrangement.
    """
    registry = _registry(tmp_path, present=("gpt-oss-20b", "Gemma4-31B"))

    assert registry.servable_names() == ("gpt-oss-20b", "Gemma4-31B")


def test_servable_names_preserves_registry_order_not_store_order(tmp_path: Path) -> None:
    """Oracle: the registry declares Qwen3.8-27B before Gemma4-31B, so servable order follows it.

    The store is created in the opposite order below. A filesystem listing would return whatever
    order the directory yields, which is not the registry's — and the sweep reports in registry
    order.
    """
    registry = _registry(tmp_path, present=("Gemma4-31B", "Qwen3.8-27B"))

    assert registry.servable_names() == ("Qwen3.8-27B", "Gemma4-31B")


def test_servable_names_is_empty_when_the_store_holds_nothing(tmp_path: Path) -> None:
    """Oracle: no weights pulled means no model can be served, so the registry contributes none."""
    registry = _registry(tmp_path)

    assert registry.servable_names() == ()


def test_a_row_with_the_wrong_column_count_is_refused(tmp_path: Path) -> None:
    """A short row would silently mis-assign every column after the missing one.

    Oracle: the format declares 8 columns, so a row with fewer cannot be read positionally.
    Failing closed with the file and line is the only outcome that lets the author fix it;
    reading it anyway would resolve a port from the SIZE column.
    """
    registry_path = tmp_path / "models.psv"
    registry_path.write_text(
        "NAME|REPO|DRAFT|PORT|SIZE|FLAGS|PARAMS|NOTE\ntruncated|repo|-|8088\n"
    )
    registry = ModelRegistry(registry_path=registry_path, store_root=tmp_path)

    with pytest.raises(MalformedRegistry) as refusal:
        registry.resolve("truncated")

    assert "models.psv:2" in str(refusal.value)


def test_the_shipped_registry_parses_without_a_store() -> None:
    """The committed registry is well-formed — checked where no store is needed to check it.

    Oracle: the registry is repo-relative, so it exists in every worktree, while the store does not
    (see ``ModelRegistry.default``). Reconciling the two must therefore skip when no weights are
    present — but the registry's SHAPE never needs weights, and folding both checks into one
    skippable test left the shape unguarded exactly where the suite normally runs. A NOTE that
    grew a stray pipe split its row into eight fields and reached a passing commit gate; the same
    registry raised MalformedRegistry the moment anything resolved a name against it. This test is
    that resolution, run unconditionally, so the refusal happens at the gate instead of in front
    of a user.
    """
    names = ModelRegistry.default().names()

    assert names, "the committed registry names no models"


def test_the_shipped_registry_names_every_model_the_store_holds() -> None:
    """The committed registry and the real store agree — no directory the registry fails to name.

    Oracle: the registry is the single source for serving a model, so a directory it does not
    name is unservable. Skipped rather than passed when no weights are present — a worktree's
    store holds only the tracked README, and an empty difference there would read as coverage
    while proving nothing.
    """
    registry = ModelRegistry.default()
    if not registry.store_root.is_dir():
        pytest.skip("no model store on this machine — nothing to reconcile")

    on_disk = {entry.name for entry in registry.store_root.iterdir() if entry.is_dir()}
    if not on_disk:
        pytest.skip("no models pulled here — the reconciliation would be vacuous")

    assert on_disk <= set(registry.names()), "store holds directories the registry does not name"


_BUDGETED_DRAFT_ROW = "\n".join(
    (
        "NAME|REPO|DRAFT|PORT|SIZE|FLAGS|PARAMS|NOTE",
        "Qwen3.8-27B|lmstudio/Qwen3.8-27B-6bit|lukaskremla/Qwen3.8-MTP|8080|22.8G"
        "|--enable-thinking|enable_thinking=false thinking_budget=256|Dense.",
        "",
    )
)


def _budgeted_draft_registry(tmp_path: Path, *, present: tuple[str, ...]) -> ModelRegistry:
    """A registry whose one row asks for a thinking budget and names a draft model."""
    registry_path = tmp_path / "models.psv"
    registry_path.write_text(_BUDGETED_DRAFT_ROW)
    store_root = tmp_path / "store"
    store_root.mkdir()
    for name in present:
        (store_root / name).mkdir()
    return ModelRegistry(registry_path=registry_path, store_root=store_root)


def test_a_thinking_budget_beside_a_pulled_draft_is_refused_at_resolution(
    tmp_path: Path,
) -> None:
    """Oracle: the installed server raises on exactly this pair, so the row can never serve.

        raise ValueError(
            "thinking_budget is not supported with speculative decoding in the server."
        )

    Refusing here rather than at the first generation is the whole point. The two columns are
    edited independently and by different motivations — one to bound a runaway reasoner, the
    other to buy decode speed — so nothing about either edit hints at the other. Left to the
    server, the fault surfaces as a failed generation on a row that was valid yesterday.
    """
    registry = _budgeted_draft_registry(tmp_path, present=("Qwen3.8-27B", "Qwen3.8-27B-MTP"))

    with pytest.raises(UnservableCombination, match="thinking_budget"):
        registry.resolve("Qwen3.8-27B")


def test_a_thinking_budget_resolves_while_the_draft_weights_are_absent(tmp_path: Path) -> None:
    """The same row serves fine until someone pulls the draft — the DRAFT column alone is inert.

    Oracle: the server's constraint is on speculative decoding actually running, and it runs off
    ``draft_path`` (weights on disk), never off ``draft_repo`` (a name that may be pulled). A
    guard keyed on the declaration instead would refuse a configuration that demonstrably works.
    """
    resolved = _budgeted_draft_registry(tmp_path, present=("Qwen3.8-27B",)).resolve("Qwen3.8-27B")

    assert resolved.draft_path is None
    assert resolved.generation_params["thinking_budget"] == 256


@pytest.mark.parametrize(
    ("draft_path", "params", "unservable"),
    [
        (Path("/store/M-MTP"), {"thinking_budget": 256}, True),
        (Path("/store/M-MTP"), {"enable_thinking": False}, False),
        (None, {"thinking_budget": 256}, False),
        (None, {}, False),
    ],
)
def test_only_a_thinking_budget_alongside_draft_weights_is_unservable(
    draft_path: Path | None, params: dict[str, object], unservable: bool
) -> None:
    """Oracle: the server refuses speculative decoding *and* a thinking budget — never either one.

    The truth table rather than the one true corner, because the predicate now answers for
    parameters that never came from a row — a probe's candidate sweep, a caller's override — and
    each of the three servable corners is a configuration something in this repo actually sends.
    A guard that fired on the draft alone would refuse every drafted model; one that fired on the
    budget alone would refuse the row the registry ships.
    """
    assert is_unservable_combination(draft_path, params) is unservable
