"""Dump JSON Schema for every public agent-only wire model.

Walks `agent_service.wire.agent`, picks every `BaseModel` subclass
(including the discriminator-tagged variants like
`PolicyVerdictApproved`), calls `model.model_json_schema(mode=
'serialization')`, and writes one JSON file per model to
`agent-service/src/agent_service/wire/schemas-agent/`.

`json-schema-to-typescript` then converts each schema into a
matching frontend TS interface.

Idempotent: the output directory is recreated each run so removed
models don't leave stale schema files behind. Adding a new model:
just define it in `wire/agent.py` and re-run `just regen-wire-types`.
"""

from __future__ import annotations

import inspect
import json
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel


def _project_root() -> Path:
    # scripts/ is at agent-service/scripts/. Up one to agent-service/,
    # one more to repo root.
    return Path(__file__).resolve().parent.parent


def _output_dir() -> Path:
    return (
        _project_root()
        / "src"
        / "agent_service"
        / "wire"
        / "schemas-agent"
    )


def _collect_models() -> list[type[BaseModel]]:
    """Walk `wire.agent`, return every concrete BaseModel subclass.

    Excludes the private base classes (`_StrictModel`, `_LenientModel`)
    and anything imported from `wire.shared` (we re-export those, but
    they're owned by the Rust pipeline; the matching TS comes from
    ts-rs, not from us)."""
    # Late import so this script can run standalone with the agent-service
    # venv activated.
    from agent_service.wire import agent as agent_mod

    shared_mod_name = "agent_service.wire.shared"
    out: list[type[BaseModel]] = []
    seen: set[str] = set()
    for name, obj in inspect.getmembers(agent_mod):
        if not inspect.isclass(obj):
            continue
        if not issubclass(obj, BaseModel):
            continue
        if obj is BaseModel:
            continue
        # Skip private bases.
        if name.startswith("_"):
            continue
        # Skip re-exports from shared (their TS comes from the Rust
        # pipeline / ts-rs, not from this script).
        if obj.__module__.startswith(shared_mod_name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(obj)
    out.sort(key=lambda m: m.__name__)
    return out


def main() -> int:
    out_dir = _output_dir()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    print(f"dumping agent schemas to {out_dir}", file=sys.stderr)
    models = _collect_models()
    if not models:
        print("no models found in wire.agent (sanity-check failure)", file=sys.stderr)
        return 1

    for model in models:
        schema = model.model_json_schema(mode="serialization")
        # Pydantic emits `title` matching the class name by default;
        # belt-and-suspenders override so json-schema-to-typescript
        # emits an interface named exactly the same.
        schema["title"] = model.__name__
        path = out_dir / f"{model.__name__}.json"
        path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}", file=sys.stderr)

    print(f"done ({len(models)} schemas)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
