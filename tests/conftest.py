from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(params=["memory", "sqlite"])
def make_saver(request: pytest.FixtureRequest) -> Callable[[], Any]:
    """A fresh checkpointer of each kind: in-memory, and SQLite (a real serialize/store round trip)."""

    def make() -> Any:
        if request.param == "memory":
            return InMemorySaver()
        return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))

    return make
