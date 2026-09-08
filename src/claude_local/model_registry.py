"""The curated registry of servable models, resolved against the store on disk.

Two concepts meet here and must not be conflated. The **model registry** is the curated,
version-controlled table: one row per model that *may* be served, naming its upstream repo,
the port it serves on, its best-quality serving flags, and any speculative-decoding draft
model. The **model store** is the machine-local directory of weights actually pulled. A row is
a claim about what is available upstream; only the store proves what is on disk, so the two
failures stay distinct — an unregistered name and an unpulled model call for different fixes.

The registry is the single source for a model's serving configuration. It is committed, so a
new model becomes servable by adding one row and pulling its weights, with no code change.

A leaf: imports nothing from the package, so every consumer can depend inward on it.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

_STORE_ROOT_ENV = "CLAUDE_LOCAL_MODELS"
"""Overrides the store location — the weights live outside any one git worktree."""

_HEADER_FIELD = "NAME"
"""First field of the column-header row, which carries no model."""

_ABSENT = "-"
"""A column with nothing to declare: no draft model, no extra serving flags."""

_FIELDS_PER_ROW = 8
"""NAME|REPO|DRAFT|PORT|SIZE|FLAGS|PARAMS|NOTE — a short row silently mis-assigns columns.

FLAGS and PARAMS are both configuration but reach the model by different routes, and the split is
load-bearing rather than cosmetic: FLAGS are command-line arguments to the server process, PARAMS
are fields in each request body. A model whose template defaults a behaviour ON cannot be talked
out of it by a server flag — the flag's absence leaves the template variable undefined, which is
not the same as false — so the only lever is the request. One column cannot express both.
"""

_DRAFT_SUFFIX = "-MTP"
"""A draft model is stored beside the model it accelerates, under this suffixed name."""

_THINKING_BUDGET = "thinking_budget"
"""The request field capping tokens spent on chain-of-thought — the hard half of non-thinking.

Named here because it is the one generation parameter this layer must recognise rather than
merely forward: it is the only one the server refuses to run in some configurations, so a
resolved model carrying it needs checking against the rest of its row.
"""


def is_unservable_combination(draft_path: Path | None, params: Mapping[str, object]) -> bool:
    """Whether mlx_vlm would refuse this pairing of draft weights and request-body fields.

    The rule ``ResolvedModel.__post_init__`` enforces on a row, exposed as a predicate because
    the row's own parameters are not the only ones asked about: a probe sweeping candidate
    parameters against a resolved model asks the same question of each candidate, and a caller
    overriding a row's parameters asks it of the override. Answering it there instead would put
    the server's rule — and the field name it turns on — under three writers.
    """
    return _THINKING_BUDGET in params and draft_path is not None


def generation_params_from_json(declared: str) -> dict[str, object]:
    """Read a command line's ``--generation-params`` value as the request-body fields it declares.

    The second surface form of the fact the PARAMS cell below declares, and it lives beside that
    one so the two cannot drift into disagreeing about what a declaration means. They differ only
    in who is writing: a registry row is hand-written into a ``|``-delimited cell, where bare
    ``key=value`` reads better than quoted JSON, while a command line is usually assembled by
    another process out of an already-parsed mapping — which is what ``json.dumps`` is for. A
    second ``key=value`` reader at that boundary would be one format with two parsers, free to
    disagree on the next quoting question.

    Raises:
        argparse.ArgumentTypeError: the value is not a JSON object. A request body is an object,
            so a list or a scalar names no fields — and it has to fail here, because a server drops
            an unrecognised body field silently and would report a whole run as configured when it
            was not.
    """
    try:
        params = json.loads(declared)
    except ValueError as malformed:
        raise argparse.ArgumentTypeError(f"not a JSON object: {malformed}") from malformed
    if not isinstance(params, dict):
        raise argparse.ArgumentTypeError(f"not a JSON object but a {type(params).__name__}")
    return params


def _generation_params(declared: str, model: str) -> Mapping[str, object]:
    """Parse a PARAMS cell into the typed request-body fields it declares.

    Values are JSON scalars, so a parameter arrives as the type the server's request schema
    expects: ``enable_thinking=false`` is the boolean ``False``, not the string ``"false"`` — and
    that distinction is the whole point, because a non-empty string is truthy in a request body
    and would silently leave the behaviour on. A value that is not valid JSON is taken verbatim,
    which is what lets a plain identifier be written without quoting it inside a ``|``-delimited
    cell.

    Raises:
        MalformedRegistry: a token carries no ``=``, so it names no field. Failing closed matters
            here more than it looks: a server ignores an unrecognised body field silently, so a
            typo accepted at this layer would read as a working configuration forever.
    """
    if declared == _ABSENT:
        return MappingProxyType({})
    params: dict[str, object] = {}
    for token in declared.split():
        key, separator, value = token.partition("=")
        if not separator:
            raise MalformedRegistry(f"{model}: generation parameter {token!r} is not key=value")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return MappingProxyType(params)


class UnknownModel(Exception):
    """The requested name is absent from the registry, so nothing can serve it."""


class ModelNotPresent(Exception):
    """The registry names the model, but its weights are not in the store."""


class MalformedRegistry(Exception):
    """A registry row does not match the declared column format."""


class UnservableCombination(Exception):
    """The row parses, but the configuration it asks for is one the server refuses to run.

    Distinct from ``MalformedRegistry``, which is a row this package cannot read. This row is
    perfectly well-formed and every cell is individually valid — it is the *pairing* that no
    server will honour, so the fault is only ever visible to something holding all the columns
    at once.
    """


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One registry row, resolved to a concrete location on this machine.

    Carries only what a consumer acts on. The registry's human-facing columns (on-disk size,
    the note explaining what a model is for) stay in the file rather than riding along here.
    """

    name: str
    repo: str
    draft_repo: str | None
    port: int
    flags: tuple[str, ...]
    generation_params: Mapping[str, object]
    """Request-body fields sent with every generation, empty when the row declares none.

    Distinct from ``flags`` by destination, not by kind: these ride in each request rather than
    on the server's command line, which is the only way to countermand a chat template's own
    default. Read-only, so one resolved model cannot be edited into a different configuration by
    a consumer that forwards it.
    """

    path: Path
    draft_path: Path | None
    """The draft model's weights, or None when the row declares one that was never pulled.

    Separate from ``draft_repo`` for the same reason the registry is separate from the store:
    the repo says what *may* be pulled, this says what is actually on disk. A consumer must
    serve from this, never from the repo id — a server handed an id it cannot find locally
    downloads it, so the two being distinct is what keeps that path unreachable.
    """

    def __post_init__(self) -> None:
        """Refuse a resolved configuration the server would raise on.

        Raises:
            UnservableCombination: the row asks for a thinking budget while a draft model is on
                disk. mlx_vlm rejects that pair outright rather than degrading — "thinking_budget
                is not supported with speculative decoding in the server" — so a row carrying
                both cannot serve a single request.
        """
        if is_unservable_combination(self.draft_path, self.generation_params):
            raise UnservableCombination(
                f"{self.name}: {_THINKING_BUDGET} cannot be sent to a server running "
                f"speculative decoding, and the draft weights at {self.draft_path} are present. "
                f"Drop one — the budget bounds a runaway reasoner, the draft buys decode rate."
            )

    def generation_params_with(
        self, override: Mapping[str, object] | None
    ) -> Mapping[str, object]:
        """The request-body fields a run should send: this row's, or an override in place of them.

        The single answer to "what configuration is this run using", because the alternative is
        each caller pairing a null check with a servability check and one of them eventually
        pairing it wrong. An override REPLACES rather than merges (D-REGISTRY-006).

        Raises:
            UnservableCombination: the override pairs a thinking budget with draft weights that
                are present. Checked here rather than at generation because the alternative is a
                cold model load ending in a fault frame — the row's own pairing is refused at
                construction for the same reason.
        """
        if override is None:
            return self.generation_params
        if is_unservable_combination(self.draft_path, override):
            raise UnservableCombination(
                f"{self.name}: the override sends {_THINKING_BUDGET}, which a server running "
                f"speculative decoding refuses, and the draft weights at {self.draft_path} are "
                f"present. Drop it from the override, or ask this of a model with no draft."
            )
        return override


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    """The registry paired with the store its rows resolve against.

    Both locations are injected so a test can resolve against a fixture registry and a scratch
    store; ``default`` supplies the real pair.
    """

    registry_path: Path
    store_root: Path

    @classmethod
    def default(cls) -> ModelRegistry:
        """The committed registry, resolved against this machine's store.

        The registry is repo-relative and therefore present in every worktree. The store is not:
        weights live in one place outside any worktree, so ``CLAUDE_LOCAL_MODELS`` overrides it
        and the repo-relative directory is only the fallback.
        """
        repo_root = Path(__file__).resolve().parents[2]
        override = os.environ.get(_STORE_ROOT_ENV, "").strip()
        return cls(
            registry_path=repo_root / "models" / "models.psv",
            store_root=Path(override) if override else repo_root / "models",
        )

    def names(self) -> tuple[str, ...]:
        """Every registered model name, in the order the registry declares them."""
        return tuple(row[0] for row in self._rows())

    def servable_names(self) -> tuple[str, ...]:
        """The registered names whose weights are on disk, in registry order.

        A registered row is a claim about what exists UPSTREAM; only the store proves what is on
        disk. This registry holds both, so which of the two a name satisfies is its fact to answer
        — a caller that re-derived it would need its own store path and its own idea of what
        "present" means, giving one fact two owners.

        A name absent from the store is omitted rather than raised on: the question here is which
        models a sweep can run, and one unpulled row is not an error in the others. Callers that
        want a specific model still go through ``resolve``, which raises ``ModelNotPresent`` and
        says where it looked.
        """
        return tuple(name for name in self.names() if self._weights_path(name).is_dir())

    def _weights_path(self, name: str) -> Path:
        """Where ``name``'s weights live in the store — the one place that layout is written.

        Both ``servable_names`` and ``resolve`` ask whether a model is present, and they must agree
        on where they looked: a second expression of the store layout would let the sweep call a
        model servable that ``resolve`` then refuses.
        """
        return self.store_root / name

    def resolve(self, name: str) -> ResolvedModel:
        """Resolve a registered name to everything needed to serve it.

        Args:
            name: A model name from the registry's NAME column.

        Returns:
            The row's serving configuration, with ``path`` pointing into the store.

        Raises:
            UnknownModel: no registry row declares this name.
            ModelNotPresent: the row exists but the weights were never pulled.
            MalformedRegistry: a registry row does not match the column format.
        """
        for row in self._rows():
            if row[0] != name:
                continue
            path = self._weights_path(name)
            if not path.is_dir():
                raise ModelNotPresent(
                    f"{name} is registered but absent from the store at {path} — pull it first"
                )
            draft_repo = None if row[2] == _ABSENT else row[2]
            draft_path = self.store_root / f"{name}{_DRAFT_SUFFIX}"
            return ResolvedModel(
                name=name,
                repo=row[1],
                draft_repo=draft_repo,
                port=int(row[3]),
                flags=() if row[5] == _ABSENT else tuple(row[5].split()),
                generation_params=_generation_params(row[6], name),
                path=path,
                # Absent unless BOTH the row declares a draft and its weights are on disk. A
                # declared-but-unpulled draft is the common case (the registry names one for
                # Qwen3.8-27B that was never pulled), and it must read as "no draft" rather
                # than as a repo id a server would try to fetch.
                draft_path=draft_path if draft_repo and draft_path.is_dir() else None,
            )
        raise UnknownModel(f"no registry row for {name!r}; available: {', '.join(self.names())}")

    def _rows(self) -> Iterator[tuple[str, ...]]:
        """Yield each model row, skipping comments, blanks, and the column header."""
        for number, line in enumerate(self.registry_path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = tuple(field.strip() for field in stripped.split("|"))
            if fields[0] == _HEADER_FIELD:
                continue
            if len(fields) != _FIELDS_PER_ROW:
                raise MalformedRegistry(
                    f"{self.registry_path}:{number} has {len(fields)} fields, "
                    f"expected {_FIELDS_PER_ROW}"
                )
            yield fields
