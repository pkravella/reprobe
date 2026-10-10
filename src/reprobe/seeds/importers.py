"""R14: import seeds from the attack catalogs that already exist.

The PRD's positioning is explicit: Promptfoo already has the attacks, so
Reprobe consumes them rather than rewriting them. Each importer normalises into
`Seed` and keeps `source`, so attribution survives into a finding's report.

The formats were read off the projects' own published files, not their docs:

* **Promptfoo** -- a *generated* red-team file (`promptfoo redteam generate`):
  `tests[].vars[<injectVar>]` holds the attack, `metadata.pluginId` names it,
  `metadata.strategyId` the strategy that transformed it. `injectVar` is
  `redteam.injectVar`, default `prompt`; other vars are context, not attacks.
  A `promptfooconfig.yaml` alone lists plugins but contains no attack text
  until generation runs, and importing one says so.
* **AgentDojo** -- its injection tasks are Python classes, not JSON. The JSON
  it publishes is run logs, whose `injections` map holds the rendered attack
  text per injection point. A file or a directory of logs; logs of the `none`
  attack inject nothing and are skipped.
* **JSONL** -- `{"text", "family"?, "source"?, "tags"?}` per line, for any other
  corpus.

**Text is kept verbatim.** These corpora aim at their own goals -- AgentDojo's
are bank transfers and email addresses -- not at Reprobe's `{{ canary_path }}`,
and rewriting them would change what the upstream attack tests. So a seed
without the token is tagged `untargeted`: it exercises an injection's framing,
and reaches a violation only if the agent wanders into one. The CLI says how
many there are, so a search cannot spend its budget on them unannounced.

**Nothing is skipped silently.** A malformed line, a log that is not a log, an
import that finds nothing: each is a `SeedError` naming where. A seed that was
dropped on the way in is one that silently never ran.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from reprobe.errors import SeedError
from reprobe.ids import digest
from reprobe.seeds import CANARY_PATH_TOKEN, Seed

UNTARGETED_TAG = "untargeted"


def _prefix(source: str) -> str:
    """The part of `source` before any `:`, made safe for an id."""
    root = re.sub(r"[^a-z0-9]+", "_", source.split(":", 1)[0].lower()).strip("_")
    return root or "imported"


def _seed(text: str, *, family: str, source: str, tags: Iterable[str] = ()) -> Seed:
    tags = list(tags)
    if CANARY_PATH_TOKEN not in text:
        tags.append(UNTARGETED_TAG)
    return Seed(
        id=f"{_prefix(source)}_{digest({'text': text})[:10]}",
        text=text,
        family=family,
        source=source,
        tags=tags,
    )


def _dedupe(seeds: Iterable[Seed]) -> list[Seed]:
    by_text: dict[str, Seed] = {}
    for seed in seeds:
        by_text.setdefault(seed.text.strip(), seed)
    return list(by_text.values())


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SeedError(f"cannot read {path}: {exc}") from exc


def import_promptfoo(path: Path) -> list[Seed]:
    try:
        raw = yaml.safe_load(_read(path))
    except yaml.YAMLError as exc:
        raise SeedError(f"{path} is not a promptfoo red-team file: {exc}") from exc
    if not isinstance(raw, dict):
        raise SeedError(f"{path} is not a promptfoo red-team file (expected a mapping)")
    redteam = raw.get("redteam")
    if not isinstance(redteam, dict):
        redteam = {}
    var = str(redteam.get("injectVar") or "prompt")

    seeds: list[Seed] = []
    # A config can mix ordinary tests with generated red-team ones; a test with
    # no inject var is not an attack. The empty-import error below still fires
    # when nothing is left.
    for test in raw.get("tests") or []:
        if not isinstance(test, dict):
            continue
        text = (test.get("vars") or {}).get(var)
        if not isinstance(text, str) or not text.strip():
            continue
        meta = test.get("metadata") or {}
        plugin = str(meta.get("pluginId") or "promptfoo")
        strategy = meta.get("strategyId")
        seeds.append(
            _seed(
                text,
                family=plugin,
                source=f"promptfoo:{plugin}",
                tags=[f"strategy:{strategy}"] if strategy else [],
            )
        )
    if not seeds:
        raise SeedError(
            f"no promptfoo attacks in {path}: expected generated tests with "
            f"`tests[].vars.{var}`. A promptfooconfig.yaml holds none until "
            "`promptfoo redteam generate` writes them"
        )
    return _dedupe(seeds)


def _agentdojo_logs(path: Path) -> list[Path]:
    path = Path(path)
    return sorted(path.rglob("*.json")) if path.is_dir() else [path]


def import_agentdojo(path: Path) -> list[Seed]:
    seeds: list[Seed] = []
    for log in _agentdojo_logs(path):
        try:
            record: Any = json.loads(_read(log))
        except json.JSONDecodeError as exc:
            raise SeedError(f"{log} is not an AgentDojo run log: {exc}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("injections"), dict):
            continue
        attack = str(record.get("attack_type") or "agentdojo")
        suite = str(record.get("suite_name") or "unknown")
        task = record.get("injection_task_id")
        for text in record["injections"].values():
            if isinstance(text, str) and text.strip():
                seeds.append(
                    _seed(
                        text,
                        family=attack,
                        source=f"agentdojo:{suite}/{attack}",
                        tags=[f"injection_task:{task}"] if task else [],
                    )
                )
    if not seeds:
        raise SeedError(
            f"no injections in {path}: expected AgentDojo run logs with a non-empty "
            "`injections` map (logs of the `none` attack inject nothing)"
        )
    return _dedupe(seeds)


def import_jsonl(path: Path) -> list[Seed]:
    seeds: list[Seed] = []
    for number, line in enumerate(_read(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SeedError(f"{path} line {number} is not JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise SeedError(f"{path} line {number} is not a JSON object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SeedError(f"{path} line {number} has no `text`")
        seeds.append(
            _seed(
                text,
                family=str(item.get("family") or "imported"),
                source=str(item.get("source") or "jsonl"),
                tags=[str(t) for t in item.get("tags") or []],
            )
        )
    if not seeds:
        raise SeedError(f"no seeds in {path}")
    return _dedupe(seeds)


IMPORTERS: dict[str, Callable[[Path], list[Seed]]] = {
    "promptfoo": import_promptfoo,
    "agentdojo": import_agentdojo,
    "jsonl": import_jsonl,
}


def write_seed_file(seeds: Iterable[Seed], dest: Path, *, force: bool = False) -> Path:
    """Write seeds as a file `load_seeds` reads. Refuses to replace one unless forced."""
    dest = Path(dest)
    if dest.exists() and not force:
        raise SeedError(f"{dest} exists; importing over it would drop its seeds")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.safe_dump(
            [s.model_dump() for s in seeds], sort_keys=False, allow_unicode=True, width=100
        ),
        encoding="utf-8",
    )
    return dest


__all__ = [
    "IMPORTERS",
    "UNTARGETED_TAG",
    "import_agentdojo",
    "import_jsonl",
    "import_promptfoo",
    "write_seed_file",
]
