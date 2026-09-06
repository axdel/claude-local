"""The curated catalog of servable models, resolved against the store on disk.

Two concepts meet here and must not be conflated. The **model registry** is the curated,
version-controlled catalog: one row per model that *may* be served, naming its upstream repo,
the port it serves on, its best-quality serving flags, and any speculative-decoding draft
model. The **model store** is the machine-local directory of weights actually pulled. A row is
a claim about what is available upstream; only the store proves what is on disk, so the two
failures stay distinct — an uncatalogued name and an unpulled model call for different fixes.

The catalog is the single source for a model's serving configuration. It is committed, so a
new model becomes servable by adding one row and pulling its weights, with no code change.

A leaf: imports nothing from the package, so every consumer can depend inward on it.
"""

from __future__ import annotations

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
    """The requested name is absent from the catalog, so nothing can serve it."""


class ModelNotPresent(Exception):
    """The catalog names the model, but its weights are not in the store."""


class MalformedRegistry(Exception):
    """A catalog row does not match the declared column format."""


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One catalog row, resolved to a concrete location on this machine.

    Carries only what a consumer acts on. The catalog's human-facing columns (on-disk size,
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
    default. Read-only, so one resolved row cannot be edited into a different configuration by
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


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    """The catalog paired with the store its rows resolve against.

    Both locations are injected so a test can resolve against a fixture catalog and a scratch
    store; ``default`` supplies the real pair.
    """

    registry_path: Path
    store_root: Path

    @classmethod
    def default(cls) -> ModelRegistry:
        """The committed catalog, resolved against this machine's store.

        The catalog is repo-relative and therefore present in every worktree. The store is not:
        weights live in one place outside any worktree, so ``CLAUDE_LOCAL_MODELS`` overrides it
        and the repo-relative directory is only the fallback.
        """
        repo_root = Path(__file__).resolve().parents[2]
        override = os.environ.get(_STORE_ROOT_ENV, "").strip()
        return cls(
            registry_path=repo_root / "models" / "models.tsv",
            store_root=Path(override) if override else repo_root / "models",
        )

    def names(self) -> tuple[str, ...]:
        """Every catalogued model name, in the order the catalog declares them."""
        return tuple(row[0] for row in self._rows())

    def resolve(self, name: str) -> ResolvedModel:
        """Resolve a catalogued name to everything needed to serve it.

        Args:
            name: A model name from the catalog's NAME column.

        Returns:
            The row's serving configuration, with ``path`` pointing into the store.

        Raises:
            UnknownModel: no catalog row declares this name.
            ModelNotPresent: the row exists but the weights were never pulled.
            MalformedRegistry: a catalog row does not match the column format.
        """
        for row in self._rows():
            if row[0] != name:
                continue
            path = self.store_root / name
            if not path.is_dir():
                raise ModelNotPresent(
                    f"{name} is catalogued but absent from the store at {path} — pull it first"
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
                # declared-but-unpulled draft is the common case (the catalog names one for
                # Qwen3.8-27B that was never pulled), and it must read as "no draft" rather
                # than as a repo id a server would try to fetch.
                draft_path=draft_path if draft_repo and draft_path.is_dir() else None,
            )
        raise UnknownModel(f"no catalog row for {name!r}; available: {', '.join(self.names())}")

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
