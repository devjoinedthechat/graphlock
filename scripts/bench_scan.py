"""How long `scan` takes on a large store, with the one-query path and with the checkpointer's list().

    uv run python scripts/bench_scan.py --threads 10000 [--postgres URL]

The store is built from one real paused thread whose rows are copied under new thread ids, so every
thread has the same history: `STEPS` checkpoints of a growing message log, paused at a breakpoint.
"""

from __future__ import annotations

import argparse
import itertools
import operator
import resource
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from typing_extensions import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import graphlock as gl
from graphlock import _stores

STEPS = 12


class State(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


def entry(name: str) -> Any:
    def node(_: State) -> dict[str, list[str]]:
        return {"log": [f"{name}:" + "x" * 200]}

    return node


def build(saver: Any, last: str) -> Any:
    """STEPS nodes in a row, each appending a 200-character entry; a breakpoint before `last`."""
    b = StateGraph(State)
    names = [f"step{i}" for i in range(STEPS)] + [last]
    for name in names:
        b.add_node(name, entry(name))
    b.add_edge(START, names[0])
    for a, c in itertools.pairwise(names):
        b.add_edge(a, c)
    return b.compile(checkpointer=saver, interrupt_before=[last])


def seed_sqlite(path: Path, threads: int) -> None:
    conn = sqlite3.connect(path, check_same_thread=False)
    build(SqliteSaver(conn), "review").invoke({"log": []}, {"configurable": {"thread_id": "t0"}})
    for table in ("checkpoints", "writes"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        rest = ", ".join(c for c in cols if c != "thread_id")
        conn.execute(
            "CREATE TEMP TABLE ids AS WITH RECURSIVE n(i) AS "
            f"(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < {threads - 1}) SELECT i FROM n"
        )
        conn.execute(
            f"INSERT INTO {table} (thread_id, {rest}) "
            f"SELECT 't' || i, {rest} FROM {table}, ids WHERE thread_id = 't0'"
        )
        conn.execute("DROP TABLE ids")
    conn.commit()
    conn.close()


def seed_postgres(url: str, threads: int) -> None:
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    with psycopg.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        saver = PostgresSaver(conn)
        saver.setup()
        conn.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
        build(saver, "review").invoke({"log": []}, {"configurable": {"thread_id": "t0"}})
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cols = [
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
                )
            ]
            rest = ", ".join(c for c in cols if c != "thread_id")
            conn.execute(
                f"INSERT INTO {table} (thread_id, {rest}) SELECT 't' || i, {rest} FROM {table}, "
                f"generate_series(1, {threads - 1}) AS i WHERE thread_id = 't0'"
            )


def timed(label: str, fn: Any) -> Any:
    start = time.perf_counter()
    result = fn()
    print(f"  {label:<34} {time.perf_counter() - start:7.2f}s")
    return result


def run(saver: Any, threads: int, generic: bool) -> None:
    graph = build(None, "manager_review")  # the deploy: the breakpoint node renamed
    lock = gl.extract_shape(build(None, "review"))
    report = timed("scan (one query per store)", lambda: gl.scan(graph, saver, lock=lock))
    if report.threads != threads or len(report.blocking) != threads:
        raise SystemExit(f"expected {threads} broken threads, scan reported {len(report.blocking)}")
    if generic:
        original = _stores.latest_checkpoints
        _stores.latest_checkpoints = lambda *_: None
        try:
            timed("scan (checkpointer's list())", lambda: gl.scan(graph, saver, lock=lock))
        finally:
            _stores.latest_checkpoints = original
    timed("scan --sample 1000", lambda: gl.scan(graph, saver, lock=lock, sample=1000))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=10_000)
    parser.add_argument("--postgres", help="a Postgres URL; its checkpoint tables are emptied first")
    parser.add_argument("--no-generic", action="store_true", help="skip the slow list() comparison")
    args = parser.parse_args()
    print(f"{args.threads:,} threads x {STEPS + 1} checkpoints each")
    if args.postgres:
        seed_postgres(args.postgres, args.threads)
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(args.postgres) as saver:
            print("Postgres:")
            run(saver, args.threads, not args.no_generic)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bench.sqlite"
            seed_sqlite(path, args.threads)
            print(f"SQLite ({path.stat().st_size / 1e6:.0f} MB):")
            run(
                SqliteSaver(sqlite3.connect(path, check_same_thread=False)), args.threads, not args.no_generic
            )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"  peak memory {peak / (1e6 if sys.platform == 'darwin' else 1e3):.0f} MB")


if __name__ == "__main__":
    main()
