"""The lockfile: the shape of every graph as it is deployed now, committed next to the code."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from graphlock.shape import SHAPE_VERSION, GraphShape


def _langgraph_version() -> str:
    try:
        return version("langgraph")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def dumps(shapes: dict[str, GraphShape]) -> str:
    doc: dict[str, Any] = {
        "graphlock": SHAPE_VERSION,
        "langgraph": _langgraph_version(),
        "graphs": dict(sorted(shapes.items())),
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write(path: Path, shapes: dict[str, GraphShape]) -> None:
    path.write_text(dumps(shapes))


def read(path: Path) -> dict[str, GraphShape]:
    doc = json.loads(path.read_text())
    if doc.get("graphlock") != SHAPE_VERSION:
        raise ValueError(
            f"{path} was written by a different lockfile format ({doc.get('graphlock')}); "
            f"this graphlock reads format {SHAPE_VERSION}. Re-run `graphlock lock` on the deployed code."
        )
    graphs: dict[str, GraphShape] = doc["graphs"]
    return graphs
