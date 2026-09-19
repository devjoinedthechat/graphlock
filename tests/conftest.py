from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

sys.path.insert(0, str(Path(__file__).parent))

POSTGRES_ENV = "GRAPHLOCK_TEST_POSTGRES"
POSTGRES_TABLES = "checkpoints, checkpoint_blobs, checkpoint_writes"


def _start_postgres() -> tuple[str, str] | None:
    """A throwaway Postgres in Docker: (url, container id), or None when Docker isn't available."""
    if not shutil.which("docker"):
        return None
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-e",
            "POSTGRES_PASSWORD=graphlock",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        return None
    container = run.stdout.strip()
    port = subprocess.run(
        ["docker", "port", container, "5432"], capture_output=True, text=True, check=False
    ).stdout
    url = f"postgresql://postgres:graphlock@127.0.0.1:{port.strip().rsplit(':', 1)[-1]}/postgres"
    return url, container


def _wait_for(url: str, timeout: float = 30.0) -> None:
    import psycopg

    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Postgres for the checkpointer tests: $GRAPHLOCK_TEST_POSTGRES, else a Docker container."""
    url = os.environ.get(POSTGRES_ENV)
    container = None
    if not url:
        started = _start_postgres()
        if started is None:
            pytest.skip(f"no Postgres: set {POSTGRES_ENV} or install Docker")
        url, container = started
    _wait_for(url)
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
    try:
        yield url
    finally:
        if container:
            subprocess.run(["docker", "stop", container], capture_output=True, check=False)


def postgres_saver(url: str) -> Any:
    """A PostgresSaver on an emptied store."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    conn = psycopg.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    conn.execute(f"TRUNCATE {POSTGRES_TABLES}")
    return PostgresSaver(conn)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def make_saver(request: pytest.FixtureRequest) -> Iterator[Callable[[], Any]]:
    """A fresh checkpointer of each kind.

    In-memory; SQLite, which stores whole checkpoints; and Postgres, which stores each channel
    separately, as production does.
    """
    opened: list[Any] = []
    url = request.getfixturevalue("postgres_url") if request.param == "postgres" else None

    def make() -> Any:
        if request.param == "memory":
            return InMemorySaver()
        if request.param == "sqlite":
            return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
        assert url is not None
        saver = postgres_saver(url)
        opened.append(saver.conn)
        return saver

    yield make
    for conn in opened:
        with contextlib.suppress(Exception):
            conn.close()
