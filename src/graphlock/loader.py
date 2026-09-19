"""Load graphs, migrations and checkpointers from `module:attribute` paths, and read the config."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

DEFAULT_LOCKFILE = "graphlock.json"


class ConfigError(Exception):
    """The configuration or an import path is wrong; the message says how to fix it."""


@dataclasses.dataclass
class Config:
    graphs: dict[str, str]  # name -> "module:attr"
    lockfile: Path
    migrations: str | None  # "module:attr" of a list of migrations, or of a dict name -> list
    root: Path
    scan: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)  # graph -> thread filter


def load_config(root: Path | None = None, graphs: Sequence[str] = ()) -> Config:
    """Read `[tool.graphlock]` from pyproject.toml; `graphs` ("name=module:attr") override it."""
    root = (root or Path.cwd()).resolve()
    table: dict[str, Any] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        table = tomllib.loads(pyproject.read_text()).get("tool", {}).get("graphlock", {})
    configured = dict(table.get("graphs", {}))
    for spec in graphs:
        name, sep, path = spec.partition("=")
        if not sep:
            path, name = spec, spec.rsplit(":", 1)[-1]
        configured[name] = path
    if not configured:
        raise ConfigError(
            "No graphs to check. Add them to pyproject.toml:\n\n"
            '  [tool.graphlock.graphs]\n  support = "app.graph:graph"\n\n'
            "or pass --graph support=app.graph:graph"
        )
    return Config(
        graphs=configured,
        lockfile=root / table.get("lockfile", DEFAULT_LOCKFILE),
        migrations=table.get("migrations"),
        root=root,
        scan=dict(table.get("scan", {})),
    )


def import_path(path: str, root: Path | None = None) -> Any:
    """The object at `module:attr.attr`, importing from `root` (default: the working directory)."""
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ConfigError(f"'{path}' is not a module:attribute path (e.g. app.graph:graph)")
    base = str((root or Path.cwd()).resolve())
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        obj: Any = importlib.import_module(module_name)
    except Exception as exc:
        raise ConfigError(f"Importing {module_name} failed: {type(exc).__name__}: {exc}") from exc
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise ConfigError(f"{module_name} has no attribute {attr}") from exc
    return obj


def load_graph(path: str, root: Path | None = None) -> Any:
    """A compiled graph from a path to a compiled graph, a StateGraph, or a zero-argument factory."""
    obj = import_path(path, root)
    if callable(obj) and not hasattr(obj, "builder") and not hasattr(obj, "compile"):
        obj = obj()
    if hasattr(obj, "compile") and not hasattr(obj, "builder"):
        obj = obj.compile()
    if not hasattr(obj, "builder"):
        raise ConfigError(f"{path} is not a StateGraph or a compiled StateGraph")
    return obj


def load_migrations(path: str | None, graph_name: str, root: Path | None = None) -> list[Any]:
    if not path:
        return []
    obj = import_path(path, root)
    if callable(obj) and not isinstance(obj, (list, tuple, dict)):
        obj = obj()
    if isinstance(obj, dict):
        obj = obj.get(graph_name, [])
    return list(obj)


def open_checkpointer(
    stack: contextlib.ExitStack,
    *,
    factory: str | None,
    sqlite: str | None,
    postgres: str | None,
    root: Path | None = None,
) -> Any:
    """A checkpointer from exactly one of: a factory path, a SQLite file, a Postgres URL."""
    chosen = [x for x in (factory, sqlite, postgres) if x]
    if len(chosen) != 1:
        raise ConfigError("Pass exactly one of --checkpointer module:factory, --sqlite PATH, --postgres URL")
    if sqlite:
        if not os.path.exists(sqlite):
            raise ConfigError(f"{sqlite} does not exist")
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: PLC0415 - optional
        except ImportError as exc:
            raise ConfigError("--sqlite needs langgraph-checkpoint-sqlite installed") from exc
        return stack.enter_context(SqliteSaver.from_conn_string(sqlite))
    if postgres:
        try:
            from langgraph.checkpoint.postgres import (  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415
                PostgresSaver,
            )
        except ImportError as exc:
            raise ConfigError("--postgres needs langgraph-checkpoint-postgres installed") from exc
        return stack.enter_context(PostgresSaver.from_conn_string(postgres))
    obj = import_path(factory or "", root)
    if callable(obj) and not hasattr(obj, "get_tuple"):
        obj = obj()
    if hasattr(obj, "__enter__") and not hasattr(obj, "get_tuple"):
        obj = stack.enter_context(obj)
    if not hasattr(obj, "get_tuple"):
        raise ConfigError(f"{factory} did not give a checkpointer")
    return obj
