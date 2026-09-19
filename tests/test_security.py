"""scan must not import anything because stored data names it.

Checkpoints store objects by import path. With LangGraph's default serializer, loading a checkpoint
imports those modules; with a strict allowlist it doesn't. Either way graphlock itself must add no
imports of its own: scanning a store can be no more dangerous than resuming its threads.
"""

from __future__ import annotations

import importlib
import inspect
import operator
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import pytest
from corpus import CFG
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from typing_extensions import TypedDict

import graphlock as gl

MODULE = "graphlock_side_effect_module"
SOURCE = """
import pathlib, os
pathlib.Path(os.environ["GRAPHLOCK_MARKER"]).write_text("imported")
from pydantic import BaseModel
class Order(BaseModel):
    sku: str
"""


class State(TypedDict, total=False):
    log: Annotated[list[str], operator.add]
    order: Any


def build(saver: Any, make_order: Any) -> Any:
    b = StateGraph(State)
    b.add_node("a", lambda s: {"log": ["a"], "order": make_order()})
    b.add_node("b", lambda s: {"log": ["b"]})
    b.add_edge(START, "a")
    b.add_edge("a", "b")
    return b.compile(checkpointer=saver, interrupt_before=["b"])


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A SQLite store holding an object of a module whose import leaves a marker file."""
    (tmp_path / f"{MODULE}.py").write_text(SOURCE)
    marker = tmp_path / "marker"
    monkeypatch.setenv("GRAPHLOCK_MARKER", str(marker))
    monkeypatch.syspath_prepend(str(tmp_path))
    module = __import__(MODULE)
    db = tmp_path / "store.sqlite"
    build(SqliteSaver(sqlite3.connect(db, check_same_thread=False)), lambda: module.Order(sku="X")).invoke(
        {"log": []}, CFG
    )
    del sys.modules[MODULE]
    marker.unlink()
    return db, marker


@pytest.mark.skipif(
    "allowed_msgpack_modules" not in inspect.signature(JsonPlusSerializer).parameters,
    reason="this LangGraph's serializer has no allowlist",
)
def test_scan_imports_nothing_the_serializer_blocks(store: tuple[Path, Path]) -> None:
    db, marker = store
    strict = JsonPlusSerializer(allowed_msgpack_modules=None)  # LANGGRAPH_STRICT_MSGPACK=true
    saver = SqliteSaver(sqlite3.connect(db, check_same_thread=False), serde=strict)
    report = gl.scan(build(None, lambda: None), saver)
    assert not marker.exists(), "graphlock imported a module named by stored data"
    assert MODULE not in sys.modules
    assert [(i.code, i.subject) for i in report.issues if i.code == "GL301"] == [("GL301", f"{MODULE}:Order")]


def test_scan_adds_no_imports_to_what_langgraph_loads(
    store: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the default serializer LangGraph imports the module itself; graphlock must not add to it."""
    db, _ = store
    calls: list[str] = []
    real = importlib.import_module

    def spy(name: str, package: str | None = None) -> Any:
        calls.append(name)
        return real(name, package)

    saver = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    graph = build(None, lambda: None)
    monkeypatch.setattr(importlib, "import_module", spy)
    gl.scan(graph, saver)
    langgraph_calls = list(calls)
    calls.clear()
    saver.get_tuple(CFG)  # what LangGraph alone imports to load the same checkpoint
    assert sorted(set(langgraph_calls)) == sorted(set(calls))
