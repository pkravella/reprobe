"""The seed corpus: the payload texts a search starts from.

A seed is a *template*, not a finished payload. `{{ canary_path }}` and
`{{ collector }}` are filled from the scenario by the mutation context (R6), so
one seed is portable across scenarios that plant their canary in different
places. Nothing here reaches a network or a real credential: the corpus is
published, and a seed that named a host that resolves would be a working attack
rather than a test input.

Loading is strict on purpose. A seed id travels with a candidate's lineage into
the finding it produces, so two seeds sharing an id would misattribute an
exploit. The loader refuses rather than letting the later id win.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from reprobe.errors import SeedError

#: Filled by `MutationContext` with the scenario's canary path (R6, Task 17).
CANARY_PATH_TOKEN = "{{ canary_path }}"

#: Filled with the mock collector's address, which is only routable from inside
#: the sandbox's internal network.
COLLECTOR_TOKEN = "{{ collector }}"


class Seed(BaseModel):
    """One hand-written or imported injection template.

    `source` is not decoration: the corpus mixes our own text with imported
    third-party corpora (R14), and attribution has to survive the merge.
    """

    model_config = {"frozen": True}

    id: str
    text: str
    family: str
    source: str
    tags: list[str] = Field(default_factory=list)


def load_seeds(paths: Sequence[Path] | None = None) -> list[Seed]:
    """Builtins plus every seed in `paths`, in order, with ids kept unique."""
    seeds = list(builtin_seeds())
    origin = {seed.id: "builtin" for seed in seeds}
    for raw_path in paths or []:
        path = Path(raw_path)
        for seed in _read_seed_file(path):
            if seed.id in origin:
                raise SeedError(
                    f"seed id {seed.id!r} in {path} is already defined by {origin[seed.id]}; "
                    "ids are provenance and must be unique across the corpus"
                )
            origin[seed.id] = str(path)
            seeds.append(seed)
    return seeds


def _read_seed_file(path: Path) -> list[Seed]:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SeedError(f"cannot read seed file {path}: {exc}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SeedError(
            f"seed file {path} must be a YAML list of seeds, not a {type(raw).__name__}"
        )
    seeds = []
    for index, item in enumerate(raw):
        try:
            seeds.append(Seed.model_validate(item))
        except ValidationError as exc:
            raise SeedError(f"entry {index} in {path} is not a valid seed: {exc}") from exc
    return seeds


# Last, and deliberately: `builtin` needs `Seed` at import time to build its
# tuple, so this module must be far enough along to hand it over. Re-exported
# here because `reprobe.seeds` is the corpus's public surface.
from reprobe.seeds.builtin import builtin_seeds  # noqa: E402

__all__ = [
    "CANARY_PATH_TOKEN",
    "COLLECTOR_TOKEN",
    "Seed",
    "builtin_seeds",
    "load_seeds",
]
