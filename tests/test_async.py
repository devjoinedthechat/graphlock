"""The async path: ainvoke through with_migrations, and ascan, on async SQLite and Postgres."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from conftest import POSTGRES_TABLES
from corpus import BY_ID, CFG, log_of
from test_saver import MIGRATIONS, two_breakpoints

import graphlock as gl


@pytest.fixture(params=["sqlite", "postgres"])
def async_saver(request: pytest.FixtureRequest) -> Callable[[], contextlib.AbstractAsyncContextManager[Any]]:
    url = request.getfixturevalue("postgres_url") if request.param == "postgres" else None

    @contextlib.asynccontextmanager
    async def open_saver() -> AsyncIterator[Any]:
        if url is None:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
                yield saver
            return
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        conn = await AsyncConnection.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
        try:
            await conn.execute(f"TRUNCATE {POSTGRES_TABLES}")
            yield AsyncPostgresSaver(conn)
        finally:
            await conn.close()

    return open_saver


def test_ainvoke_resumes_a_migrated_thread(async_saver: Any) -> None:
    async def run() -> list[str]:
        sc = BY_ID["rename-paused-node"]
        async with async_saver() as saver:
            await sc.v1(saver).ainvoke({"log": []}, CFG)
            graph = gl.with_migrations(sc.v2(saver), list(sc.migrations))  # type: ignore[arg-type]
            return log_of(await graph.ainvoke(None, CFG))

    assert asyncio.run(run()) == ["draft", "manager_review", "issue"]


def test_async_write_through_survives_a_second_pause(async_saver: Any) -> None:
    async def run() -> list[str]:
        async with async_saver() as saver:
            await two_breakpoints(("x", "y3"))(saver).ainvoke({"log": []}, CFG)
            graph = gl.with_migrations(two_breakpoints(("y3", "x"))(saver), MIGRATIONS)
            await graph.ainvoke(None, CFG)
            return log_of(await graph.ainvoke(None, CFG))

    assert asyncio.run(run())[-1] == "join"


def test_ascan_reports_and_repairs_like_scan(async_saver: Any) -> None:
    async def run() -> tuple[Any, Any]:
        sc = BY_ID["rename-paused-node"]
        async with async_saver() as saver:
            v1 = sc.v1(saver)
            await v1.ainvoke({"log": []}, CFG)
            lock = gl.extract_shape(v1)
            v2 = sc.v2(saver)
            raw = await gl.ascan(v2, saver, lock=lock)
            repaired = await gl.ascan(v2, saver, lock=lock, migrations=list(sc.migrations))  # type: ignore[arg-type]
            return raw, repaired

    raw, repaired = asyncio.run(run())
    assert [(i.code, i.thread_id) for i in raw.blocking] == [("GL101", "t1")]
    assert not repaired.blocking
    assert repaired.migrations_needed == {
        "rename_node('review', 'manager_review')": {"paused": 1, "finished": 0}
    }
