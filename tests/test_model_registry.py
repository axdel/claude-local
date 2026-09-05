"""Resolution tests for the model registry.

The registry is the curated catalog of models that *may* be served; the model store is the
directory of weights actually on disk. These are two concepts, and the tests hold them apart:
a name absent from the catalog and a name present in the catalog but missing from disk are
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
)

_REGISTRY_FIXTURE = """\
# A comment line, skipped.
#
NAME|REPO|DRAFT|PORT|SIZE|FLAGS|NOTE
gpt-oss-20b|mlx-community/gpt-oss-20b-Q8|-|8088|12.1G|-|Fast MoE.
Qwen3.8-27B|lmstudio/Qwen3.8-27B-6bit|lukaskremla/Qwen3.8-MTP|8080|22.8G|--enable-thinking|Dense.
Gemma4-31B|lmstudio/gemma-4-31B-6bit|-|8082|26.1G|--enable-thinking --kv-bits 8|Slowest.
"""


def _registry(tmp_path: Path, *, present: tuple[str, ...] = ()) -> ModelRegistry:
    """Build a registry over a fixture catalog and a store holding ``present`` models."""
    registry_path = tmp_path / "models.tsv"
    registry_path.write_text(_REGISTRY_FIXTURE)
    store_root = tmp_path / "store"
    store_root.mkdir()
    for name in present:
        (store_root / name).mkdir()
    return ModelRegistry(registry_path=registry_path, store_root=store_root)


def test_resolve_returns_the_catalog_row_and_the_store_path(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("gpt-oss-20b",)).resolve("gpt-oss-20b")

    # Oracle: the fixture's own row declares each value; nothing here was read off the parser.
    assert resolved.repo == "mlx-community/gpt-oss-20b-Q8"
    assert resolved.port == 8088
    assert resolved.draft_repo is None  # a "-" in the DRAFT column means no draft model
    assert resolved.path == tmp_path / "store" / "gpt-oss-20b"


def test_resolve_carries_the_draft_model_when_the_row_names_one(tmp_path: Path) -> None:
    resolved = _registry(tmp_path, present=("Qwen3.8-27B",)).resolve("Qwen3.8-27B")

    assert resolved.draft_repo == "lukaskremla/Qwen3.8-MTP"


def test_a_declared_draft_whose_weights_are_absent_resolves_to_no_draft_path(
    tmp_path: Path,
) -> None:
    """A draft named in the catalog but never pulled must not read as servable.

    Oracle: the registry/store split — the DRAFT column says what may be pulled, only the store
    proves what is on disk. A consumer handed the repo id for absent weights downloads them, so
    the path staying None is what keeps that branch unreachable. This is the real shipped state:
    the catalog names a draft for Qwen3.8-27B whose weights are not in the store.
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
    catalog can assert. Inferring one from a directory name would serve an unvalidated pairing.
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
    """A name absent from the catalog cannot be served, and the refusal must be actionable."""
    registry = _registry(tmp_path, present=("gpt-oss-20b",))

    with pytest.raises(UnknownModel) as refusal:
        registry.resolve("Llama-99B")

    # Oracle: a bare "unknown model" leaves the caller guessing; the available names are the
    # only thing that makes the error actionable, so they are part of the contract.
    assert "Llama-99B" in str(refusal.value)
    assert "gpt-oss-20b" in str(refusal.value)


def test_a_catalogued_model_absent_from_the_store_is_a_distinct_refusal(tmp_path: Path) -> None:
    """Catalogued-but-not-pulled is not the same failure as unknown, and must not be conflated.

    Oracle: the registry lists what *may* be pulled, so a row proves nothing about the disk.
    The two cases call for different fixes — add a row vs pull the weights — so a caller that
    cannot tell them apart cannot act on either.
    """
    registry = _registry(tmp_path, present=())  # catalogued, but nothing on disk

    with pytest.raises(ModelNotPresent) as refusal:
        registry.resolve("gpt-oss-20b")

    assert "gpt-oss-20b" in str(refusal.value)


def test_names_lists_the_catalog_skipping_comments_and_the_header(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    # Oracle: the fixture declares 3 model rows behind 2 comment lines and 1 header line.
    assert registry.names() == ("gpt-oss-20b", "Qwen3.8-27B", "Gemma4-31B")


def test_a_row_with_the_wrong_column_count_is_refused(tmp_path: Path) -> None:
    """A short row would silently mis-assign every column after the missing one.

    Oracle: the format declares 7 columns, so a row with fewer cannot be read positionally.
    Failing closed with the file and line is the only outcome that lets the author fix it;
    reading it anyway would resolve a port from the SIZE column.
    """
    registry_path = tmp_path / "models.tsv"
    registry_path.write_text("NAME|REPO|DRAFT|PORT|SIZE|FLAGS|NOTE\ntruncated|repo|-|8088\n")
    registry = ModelRegistry(registry_path=registry_path, store_root=tmp_path)

    with pytest.raises(MalformedRegistry) as refusal:
        registry.resolve("truncated")

    assert "models.tsv:2" in str(refusal.value)


def test_the_shipped_registry_catalogues_every_model_the_store_holds() -> None:
    """The committed catalog and the real store agree — no directory the catalog fails to name.

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
